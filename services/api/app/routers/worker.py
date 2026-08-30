"""Desktop AI worker API.

The ONLY interface between the RTX 4070 SUPER desktop and the VPS. The desktop makes
outbound HTTPS requests here; the VPS never dials the desktop (ADR-0001).

Claiming is a single ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)``,
which is correct under concurrency without a distributed lock. Leases expire, and a
reaper on the ``maintain`` queue returns abandoned work to the queue -- so a desktop
that loses power mid-job costs us one lease interval, not a stuck pipeline.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.deps import SessionDep, WorkerDep
from thedrop_database.enums import JobStatus, WorkerStatus
from thedrop_database.models import Job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/worker", tags=["worker"])

_DEFAULT_LEASE_SECONDS = 900
_HEARTBEAT_GRACE_SECONDS = 90


class HeartbeatRequest(BaseModel):
    status: str = Field(default=WorkerStatus.ONLINE, max_length=16)
    current_job_count: int = Field(default=0, ge=0)
    gpu_name: str | None = Field(default=None, max_length=128)
    gpu_vram_free_mb: int | None = Field(default=None, ge=0)
    agent_version: str | None = Field(default=None, max_length=32)
    capabilities: dict[str, Any] | None = None


class ClaimRequest(BaseModel):
    #: Only job types this runner advertises are ever leased to it, so a runner
    #: without a GPU never receives an image job.
    handlers: list[str] = Field(min_length=1, max_length=32)
    max_jobs: int = Field(default=1, ge=1, le=4)
    lease_seconds: int = Field(default=_DEFAULT_LEASE_SECONDS, ge=60, le=7200)


class CompleteRequest(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)


class FailRequest(BaseModel):
    error: str = Field(max_length=4000)
    retryable: bool = True


@router.post("/heartbeat")
def heartbeat(
    payload: HeartbeatRequest, node: WorkerDep, db: SessionDep, request: Request
) -> dict[str, Any]:
    node.last_heartbeat_at = datetime.now(UTC)
    node.status = payload.status
    node.current_job_count = payload.current_job_count
    node.gpu_name = payload.gpu_name or node.gpu_name
    node.gpu_vram_free_mb = payload.gpu_vram_free_mb
    node.agent_version = payload.agent_version or node.agent_version
    node.ip_last_seen = request.client.host if request.client else None
    if payload.capabilities is not None:
        node.capabilities = payload.capabilities

    # Extend leases held by this node in the same round trip, so a healthy runner
    # never loses work to the reaper.
    db.execute(
        update(Job)
        .where(Job.leased_by_id == node.id, Job.status == JobStatus.LEASED)
        .values(
            heartbeat_at=datetime.now(UTC),
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=_DEFAULT_LEASE_SECONDS),
        )
    )
    db.commit()

    return {"status": "ok", "serverTime": datetime.now(UTC).isoformat()}


@router.post("/jobs/claim")
def claim_jobs(
    payload: ClaimRequest, node: WorkerDep, db: SessionDep
) -> dict[str, list[dict[str, Any]]]:
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=payload.lease_seconds)

    # SKIP LOCKED means two runners claiming simultaneously never collide and never
    # block each other.
    candidates = (
        select(Job.id)
        .where(
            Job.status == JobStatus.QUEUED,
            Job.job_type.in_(payload.handlers),
            Job.available_at <= now,
        )
        .order_by(Job.priority.desc(), Job.available_at)
        .limit(payload.max_jobs)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    claimed_ids = db.scalars(
        update(Job)
        .where(Job.id.in_(candidates))
        .values(
            status=JobStatus.LEASED,
            leased_by_id=node.id,
            leased_at=now,
            lease_expires_at=lease_until,
            heartbeat_at=now,
            attempts=Job.attempts + 1,
        )
        .returning(Job.id)
    ).all()
    db.commit()

    if not claimed_ids:
        return {"jobs": []}

    jobs = db.scalars(select(Job).where(Job.id.in_(claimed_ids))).all()
    logger.info("jobs claimed", extra={"worker": node.name, "count": len(jobs)})

    return {
        "jobs": [
            {
                "id": str(job.public_id),
                "jobType": job.job_type,
                "payload": job.payload,
                "attempts": job.attempts,
                "maxAttempts": job.max_attempts,
                "leaseExpiresAt": job.lease_expires_at.isoformat()
                if job.lease_expires_at
                else None,
                "idempotencyKey": job.idempotency_key,
            }
            for job in jobs
        ]
    }


def _load_leased_job(db: SessionDep, node_id: int, job_public_id: str) -> Job:
    job = db.scalar(select(Job).where(Job.public_id == job_public_id))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.leased_by_id != node_id:
        # Almost always means the lease expired and was reaped, then re-leased.
        raise HTTPException(status.HTTP_409_CONFLICT, "Job is not leased to this worker")
    return job


@router.post("/jobs/{job_id}/complete")
def complete_job(
    job_id: str, payload: CompleteRequest, node: WorkerDep, db: SessionDep
) -> dict[str, str]:
    job = _load_leased_job(db, node.id, job_id)

    # Completing an already-finished job is a no-op, not an error: a runner that
    # finished exactly as its lease expired must not be able to double-apply a result.
    if job.status == JobStatus.DONE:
        return {"status": "already_complete"}

    job.status = JobStatus.DONE
    job.result = payload.result
    job.completed_at = datetime.now(UTC)
    job.error = None
    db.commit()
    return {"status": "ok"}


@router.post("/jobs/{job_id}/fail")
def fail_job(
    job_id: str, payload: FailRequest, node: WorkerDep, db: SessionDep
) -> dict[str, Any]:
    job = _load_leased_job(db, node.id, job_id)

    retry = payload.retryable and job.attempts < job.max_attempts
    if retry:
        # Exponential backoff, capped. Prevents a failing provider or model from
        # being hammered every few seconds.
        delay = min(60 * (2 ** (job.attempts - 1)), 3600)
        job.status = JobStatus.QUEUED
        job.available_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.leased_by_id = None
        job.leased_at = None
        job.lease_expires_at = None
    else:
        job.status = JobStatus.FAILED
        job.completed_at = datetime.now(UTC)

    job.error = payload.error
    db.commit()

    logger.warning(
        "job failed", extra={"job_type": job.job_type, "retry": retry, "attempts": job.attempts}
    )
    return {"status": "queued" if retry else "failed", "attempts": job.attempts}


@router.get("/status")
def worker_status(node: WorkerDep, db: SessionDep) -> dict[str, Any]:
    """Lets the runner confirm its own view of the world matches the server's."""
    leased = db.scalars(
        select(Job).where(Job.leased_by_id == node.id, Job.status == JobStatus.LEASED)
    ).all()
    return {
        "name": node.name,
        "status": node.status,
        "leasedJobs": [str(j.public_id) for j in leased],
        "heartbeatGraceSeconds": _HEARTBEAT_GRACE_SECONDS,
        "serverTime": datetime.now(UTC).isoformat(),
    }
