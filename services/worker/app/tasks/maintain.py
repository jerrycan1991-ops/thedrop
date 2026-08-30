"""Housekeeping tasks.

Cheap, frequent, VPS-local. Nothing here loads a model or touches a GPU.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from thedrop_database import session_scope
from thedrop_database.enums import JobStatus, WorkerStatus
from thedrop_database.models import Job, Provider, WorkerNode

from app.celery_app import celery_app

logger = logging.getLogger(__name__)

#: A worker that has not checked in for this long is presumed gone. Two missed
#: 30-second heartbeats plus a little slack.
STALE_HEARTBEAT_SECONDS = 90


@celery_app.task(name="app.tasks.maintain.reap_expired_leases")
def reap_expired_leases() -> dict[str, int]:
    """Return abandoned jobs to the queue.

    A desktop that loses power mid-job holds its lease until it expires. This returns
    that work to ``queued`` with backoff, or marks it failed once attempts are spent.
    Without this task, an offline desktop would strand every job it had claimed.
    """
    now = datetime.now(UTC)
    requeued = 0
    failed = 0

    with session_scope() as db:
        expired = db.scalars(
            select(Job).where(
                Job.status == JobStatus.LEASED,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < now,
            )
        ).all()

        for job in expired:
            if job.attempts < job.max_attempts:
                delay = min(60 * (2 ** max(job.attempts - 1, 0)), 3600)
                job.status = JobStatus.QUEUED
                job.available_at = now + timedelta(seconds=delay)
                job.leased_by_id = None
                job.leased_at = None
                job.lease_expires_at = None
                job.error = "lease expired without heartbeat"
                requeued += 1
            else:
                job.status = JobStatus.FAILED
                job.completed_at = now
                job.error = "lease expired; attempts exhausted"
                failed += 1

    if requeued or failed:
        logger.info("leases reaped", extra={"requeued": requeued, "failed": failed})
    return {"requeued": requeued, "failed": failed}


@celery_app.task(name="app.tasks.maintain.mark_stale_workers_offline")
def mark_stale_workers_offline() -> dict[str, int]:
    """Flip silent workers to OFFLINE so the admin dashboard tells the truth."""
    cutoff = datetime.now(UTC) - timedelta(seconds=STALE_HEARTBEAT_SECONDS)

    with session_scope() as db:
        result = db.execute(
            update(WorkerNode)
            .where(
                WorkerNode.status != WorkerStatus.OFFLINE,
                (WorkerNode.last_heartbeat_at.is_(None))
                | (WorkerNode.last_heartbeat_at < cutoff),
            )
            .values(status=WorkerStatus.OFFLINE, current_job_count=0)
        )
        count = result.rowcount or 0

    if count:
        logger.warning("workers marked offline", extra={"count": count})
    return {"marked_offline": count}


@celery_app.task(name="app.tasks.maintain.reset_provider_quotas")
def reset_provider_quotas() -> dict[str, int]:
    with session_scope() as db:
        result = db.execute(update(Provider).values(quota_used_today=0))
    return {"reset": result.rowcount or 0}


@celery_app.task(name="app.tasks.maintain.heartbeat")
def heartbeat() -> dict[str, str]:
    """Proof of life for the worker itself, surfaced on the System Health page."""
    return {"status": "ok", "at": datetime.now(UTC).isoformat()}
