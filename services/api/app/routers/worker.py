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
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from thedrop_database.clustering import resync_story_entities
from thedrop_database.enums import EntityType, JobStatus, WorkerStatus
from thedrop_database.models import Entity, Job, RawArticle, RawArticleEntity

from app.deps import SessionDep, SettingsDep, WorkerDep

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


#: How far a unit vector may drift before it is rejected. bge-small-en-v1.5 returns
#: normalized vectors; anything meaningfully off the unit sphere came from a different
#: model or a different pooling strategy, whatever it calls itself. Loose enough to
#: absorb float32 round-tripping through JSON.
_NORM_TOLERANCE = 0.05


class EmbeddingItem(BaseModel):
    id: str = Field(max_length=64)
    vector: list[float]


class EmbeddingsRequest(BaseModel):
    """Vectors computed on the desktop, on their way into raw_articles.

    `model` is not decoration. ADR-0005 keeps ONE vector space, and mixing models or
    dimensions corrupts similarity search in a way that is very hard to notice later --
    nothing errors, results just quietly get worse. So the model is declared, checked
    against config, and the batch is refused if it disagrees.
    """

    model: str = Field(max_length=128)
    items: list[EmbeddingItem] = Field(min_length=1, max_length=128)


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(default="OTHER", max_length=16)
    mentions: int = Field(default=1, ge=0)
    salience: float | None = Field(default=None, ge=0, le=1)


class ArticleEntities(BaseModel):
    id: str = Field(max_length=64)
    #: An EMPTY list is meaningful and must be sent: it says extraction ran and found
    #: nothing, which is what stops the article being re-queued forever. Omitting the
    #: article instead would be indistinguishable from never processing it.
    entities: list[ExtractedEntity] = Field(max_length=64)


class EntitiesRequest(BaseModel):
    model: str = Field(max_length=128)
    items: list[ArticleEntities] = Field(min_length=1, max_length=64)


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
def fail_job(job_id: str, payload: FailRequest, node: WorkerDep, db: SessionDep) -> dict[str, Any]:
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


@router.post("/embeddings")
def store_embeddings(
    payload: EmbeddingsRequest, node: WorkerDep, db: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Write desktop-computed vectors into raw_articles.

    A separate endpoint rather than a field on `/jobs/{id}/complete`, for two reasons:
    `jobs.result` is kept forever, so vectors posted through it would duplicate every
    embedding into the jobs table permanently; and the lease protocol stays generic
    instead of growing a dispatch table keyed on job type.

    Posting is therefore separate from completing, and deliberately ordered: the runner
    stores vectors first, then completes. If it dies in between, the lease expires, the
    job is requeued, and the same vectors are written again -- identical values, so the
    retry is a no-op rather than a correction.

    The worker token is the trust boundary, as it already is for claiming and
    completing. What this adds is that a *wrong* vector is refused even from a
    legitimate worker.
    """
    if payload.model != settings.ai.embedding_model:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"embedding model mismatch: this deployment stores "
            f"{settings.ai.embedding_model!r}, worker sent {payload.model!r}",
        )

    expected_dimensions = settings.ai.embedding_dimensions
    for item in payload.items:
        if len(item.vector) != expected_dimensions:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{item.id}: expected {expected_dimensions} dimensions, got {len(item.vector)}",
            )
        norm = math.sqrt(sum(value * value for value in item.vector))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{item.id}: vector is not normalized (norm {norm:.4f})",
            )

    # Nothing is written until every item has passed. A partially applied batch would
    # leave the caller unable to say what happened without re-reading each row.
    now = datetime.now(UTC)
    stored: list[str] = []
    unknown: list[str] = []
    for item in payload.items:
        updated = db.execute(
            update(RawArticle)
            .where(RawArticle.public_id == item.id)
            .values(embedding=item.vector, embedded_at=now)
            .returning(RawArticle.id)
        ).scalar()
        (stored if updated is not None else unknown).append(item.id)
    db.commit()

    if unknown:
        # Not an error: a row can legitimately vanish between dispatch and completion.
        # Reported so a runner that is systematically wrong is visible rather than
        # silently writing nothing.
        logger.warning(
            "embeddings for unknown articles",
            extra={"worker": node.name, "count": len(unknown)},
        )

    logger.info("embeddings stored", extra={"worker": node.name, "count": len(stored)})
    return {"stored": len(stored), "unknown": unknown}


@router.post("/entities")
def store_entities(payload: EntitiesRequest, node: WorkerDep, db: SessionDep) -> dict[str, Any]:
    """Write desktop-extracted entities, resolving each name to a shared entity row.

    Resolution by (canonical_name, entity_type) is what lets the clustering guard match
    on `entity_id` rather than on approximate string equality. "Jerome Powell" is one
    row whether it arrived on an article or was promoted onto a story.

    An article's entity set is REPLACED, not merged. Re-extraction happens after a
    model change or a requeued job, and merging would leave the output of two different
    models mixed together in one article with no way to tell which came from where.

    If the article already belongs to a story, that story's promoted entity set is
    RESYNCED after the replacement. Found in production: without this, a story kept a
    "United States" entity in its guard set for a Nepal floods story after the one
    member article that had carried it was re-extracted and no longer did -- a ghost
    entity with no current article behind it, sitting in the exact table
    `story_guard_entities` reads for the live clustering join decision.

    `entities_extracted_at` is set from the presence of the article in this payload,
    not from whether any entities came back -- an article where the tagger found
    nothing is processed, and must not be queued again.
    """
    known_types = {t.value for t in EntityType}
    now = datetime.now(UTC)
    stored = 0
    unknown: list[str] = []
    # Articles that already belong to a story have that story's promoted entity set
    # go stale the moment their own raw_article_entities are replaced below --
    # resync_story_entities is what closes that, but it is a full recompute, so it
    # runs at most once per affected story for this whole batch, not once per article.
    stories_to_resync: set[int] = set()

    for item in payload.items:
        article_id, story_id = db.execute(
            select(RawArticle.id, RawArticle.story_id).where(RawArticle.public_id == item.id)
        ).one_or_none() or (None, None)
        if article_id is None:
            unknown.append(item.id)
            continue
        if story_id is not None:
            stories_to_resync.add(story_id)

        db.execute(delete(RawArticleEntity).where(RawArticleEntity.raw_article_id == article_id))

        for extracted in item.entities:
            entity_type = extracted.type if extracted.type in known_types else EntityType.OTHER
            # ON CONFLICT DO UPDATE rather than DO NOTHING: `returning` yields no row on
            # a plain DO NOTHING, so a second article mentioning a known entity would
            # look like a failed insert and lose the link.
            entity_id = db.execute(
                pg_insert(Entity)
                .values(canonical_name=extracted.name, entity_type=entity_type)
                .on_conflict_do_update(
                    constraint="uq_entities_name_type",
                    set_={"canonical_name": extracted.name},
                )
                .returning(Entity.id)
            ).scalar_one()

            db.execute(
                pg_insert(RawArticleEntity)
                .values(
                    raw_article_id=article_id,
                    entity_id=entity_id,
                    salience=extracted.salience,
                    mention_count=extracted.mentions,
                )
                .on_conflict_do_nothing(constraint="uq_raw_article_entities_pair")
            )
            stored += 1

        db.execute(
            update(RawArticle).where(RawArticle.id == article_id).values(entities_extracted_at=now)
        )

    for story_id in stories_to_resync:
        resync_story_entities(db, story_id)

    db.commit()

    if unknown:
        logger.warning(
            "entities for unknown articles",
            extra={"worker": node.name, "count": len(unknown)},
        )
    logger.info(
        "entities stored",
        extra={"worker": node.name, "articles": len(payload.items), "entities": stored},
    )
    return {"articles": len(payload.items) - len(unknown), "entities": stored, "unknown": unknown}


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
