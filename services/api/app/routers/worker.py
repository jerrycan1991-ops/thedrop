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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from thedrop_database.clustering import resync_story_entities
from thedrop_database.enums import EntityType, JobStatus, RiskTier, WorkerStatus
from thedrop_database.models import (
    AiRun,
    Claim,
    ClaimEvidence,
    Entity,
    Job,
    RawArticle,
    RawArticleEntity,
    Story,
)

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


class ExtractedClaimEvidence(BaseModel):
    source_article_id: str = Field(max_length=64)
    quote: str = Field(min_length=1, max_length=4000)


class ExtractedClaimItem(BaseModel):
    claim_text: str = Field(min_length=1, max_length=2000)
    claim_type: str = Field(max_length=24)
    attributed_to: str | None = Field(default=None, max_length=255)
    confidence: int = Field(ge=0, le=100)
    evidence: list[ExtractedClaimEvidence] = Field(min_length=1, max_length=16)


class StoryClaims(BaseModel):
    """One story's extraction result, or its failure.

    `error` and `claims` are mutually exclusive in practice (agent.claims.extract()
    either returns a validated result or raises), but both are optional here rather
    than one being required -- a story that genuinely found zero claims is not an
    error, and must still mark `claims_extracted_at` the same way an unknown article
    still marks `entities_extracted_at` in `store_entities`.
    """

    model_config = {"populate_by_name": True}

    story_id: str = Field(max_length=64, alias="storyId")
    claims: list[ExtractedClaimItem] = Field(default_factory=list, max_length=64)
    injection_detected: bool = Field(default=False, alias="injectionDetected")
    risk_tier: str | None = Field(default=None, max_length=16, alias="riskTier")
    risk_reasons: list[str] = Field(default_factory=list, alias="riskReasons")
    error: str | None = Field(default=None, max_length=4000)


class ClaimsRequest(BaseModel):
    model: str = Field(max_length=128)
    items: list[StoryClaims] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _successful_items_carry_a_risk_tier(self) -> ClaimsRequest:
        # No default on a missing risk_tier, mirroring agent.claims.ExtractionResult's
        # own rule: silently treating an absent assessment as "standard" would defeat
        # the whole point of asking for one. A story reported as failed (item.error
        # set) legitimately has no risk_tier -- extraction never produced a result.
        for item in self.items:
            if item.error is None and item.risk_tier is None:
                raise ValueError(f"{item.story_id}: risk_tier is required when error is not set")
        return self


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


def _parse_uuid(value: str) -> uuid.UUID | None:
    """A public_id that fails to parse is "not found", not a crash.

    Comparing a UUID column against a malformed string raises at the database level and
    leaves the transaction unusable for anything that runs after it in the same
    request -- unlike an ordinary "no row matched", which is silent. `source_article_id`
    values here are model output, not a guaranteed-well-formed system value, so a
    malformed one (truncation, a hallucinated id) is a real possibility this endpoint
    processes many items per request, and one bad id must not take the rest down with
    it. Validated in Python first, before it ever reaches a query.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


@router.post("/claims")
def store_claims(payload: ClaimsRequest, node: WorkerDep, db: SessionDep) -> dict[str, Any]:
    """Write desktop-extracted claims (PIPELINE.md §10-11).

    A story's `claims_extracted_at` is set on EVERY attempt in this payload, success or
    failure -- see the migration that added the column. A story reported with `error`
    gets an `ai_runs` row (status `invalid_output`) and nothing else; its existing
    claims, if any, are left untouched rather than cleared, since a failed re-attempt
    should not delete a previously-successful extraction.

    A successful story's claim set is REPLACED, not merged -- same reasoning as
    `store_entities`: re-extraction after a prompt change or a requeued job must not
    leave two different runs' claims mixed in one story with no way to tell which came
    from where. `ClaimEvidence` cascades on `claim_id`, so deleting a story's `claims`
    rows cleans up their evidence automatically.

    `attributed_to` is resolved to an `entities` row the same way `store_entities`
    resolves NER output: by (canonical_name, entity_type), upserted. Always typed
    OTHER here -- extraction does not classify who it is, only that a claim named
    them, and OTHER is honest about that rather than guessing a type nobody predicted.

    A claim whose evidence entries ALL cite an article we don't recognise is dropped
    entirely rather than stored with no evidence: DATABASE.md is explicit that
    `claim_evidence` is what makes a claim auditable, not decorative, so a claim with
    none of it stored would be an assertion with nothing behind it.

    `provider` is inferred from the model name (`claude-` prefix vs. anything else)
    rather than sent explicitly -- the desktop's generic embeddings/entities/claims
    delivery mechanism posts a fixed (model, items) pair per deliverable type, and
    adding a third field there for this alone was not worth the wider protocol change.
    """
    provider = "anthropic" if payload.model.startswith("claude-") else "ollama"
    now = datetime.now(UTC)
    unknown_stories: list[str] = []
    stored_claims = 0
    failed_stories = 0

    for item in payload.items:
        parsed_story_id = _parse_uuid(item.story_id)
        story_id = (
            db.scalar(select(Story.id).where(Story.public_id == parsed_story_id))
            if parsed_story_id is not None
            else None
        )
        if story_id is None:
            unknown_stories.append(item.story_id)
            continue

        db.execute(update(Story).where(Story.id == story_id).values(claims_extracted_at=now))

        if item.error is not None:
            failed_stories += 1
            db.add(
                AiRun(
                    story_id=story_id,
                    purpose="extract",
                    provider=provider,
                    model=payload.model,
                    status="invalid_output",
                    error=item.error[:4000],
                )
            )
            continue

        db.execute(delete(Claim).where(Claim.story_id == story_id))
        db.execute(
            update(Story)
            .where(Story.id == story_id)
            .values(
                risk_tier=item.risk_tier or RiskTier.STANDARD,
                risk_reasons=item.risk_reasons,
            )
        )

        for extracted in item.claims:
            attributed_to_id: int | None = None
            if extracted.attributed_to:
                attributed_to_id = db.execute(
                    pg_insert(Entity)
                    .values(canonical_name=extracted.attributed_to, entity_type=EntityType.OTHER)
                    .on_conflict_do_update(
                        constraint="uq_entities_name_type",
                        set_={"canonical_name": extracted.attributed_to},
                    )
                    .returning(Entity.id)
                ).scalar_one()

            resolved_evidence: list[tuple[int, int, str, str]] = []
            source_ids: set[int] = set()
            first_asserted_at: datetime | None = None
            for ev in extracted.evidence:
                parsed_article_id = _parse_uuid(ev.source_article_id)
                row = (
                    db.execute(
                        select(
                            RawArticle.id,
                            RawArticle.source_id,
                            RawArticle.canonical_url,
                            RawArticle.published_at,
                        ).where(RawArticle.public_id == parsed_article_id)
                    ).one_or_none()
                    if parsed_article_id is not None
                    else None
                )
                if row is None:
                    continue
                raw_article_id, source_id, url, published_at = row
                resolved_evidence.append((raw_article_id, source_id, url, ev.quote))
                source_ids.add(source_id)
                if published_at is not None and (
                    first_asserted_at is None or published_at < first_asserted_at
                ):
                    first_asserted_at = published_at

            if not resolved_evidence:
                # Every cited article is unknown to us -- nothing left to attach this
                # claim to. Dropped, not stored bare: see the docstring.
                continue

            claim = Claim(
                story_id=story_id,
                claim_text=extracted.claim_text,
                claim_type=extracted.claim_type,
                attributed_to_entity_id=attributed_to_id,
                confidence=extracted.confidence,
                supporting_source_count=len(source_ids),
                first_asserted_at=first_asserted_at,
            )
            db.add(claim)
            db.flush()

            for raw_article_id, source_id, url, quote in resolved_evidence:
                db.add(
                    ClaimEvidence(
                        claim_id=claim.id,
                        raw_article_id=raw_article_id,
                        source_id=source_id,
                        quote=quote,
                        url=url,
                        stance="supports",
                    )
                )
            stored_claims += 1

        db.add(
            AiRun(
                story_id=story_id,
                purpose="extract",
                provider=provider,
                model=payload.model,
                status="ok",
                response_meta={"injectionDetected": item.injection_detected},
            )
        )

    db.commit()

    if unknown_stories:
        logger.warning(
            "claims for unknown stories",
            extra={"worker": node.name, "count": len(unknown_stories)},
        )
    logger.info(
        "claims stored",
        extra={
            "worker": node.name,
            "stories": len(payload.items) - len(unknown_stories),
            "claims": stored_claims,
            "failed": failed_stories,
        },
    )
    return {
        # "stored" matches embeddings'/entities' contract -- runner.py's generic
        # delivery mechanism reads this key from every deliverable's response
        # uniformly. Here it counts stories processed, the item-level unit for this
        # endpoint, the same way "stored" counts articles for the other two.
        "stored": len(payload.items) - len(unknown_stories),
        "claims": stored_claims,
        "failed": failed_stories,
        "unknown": unknown_stories,
    }


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
