"""Turn stories ready for claim extraction into work orders for the desktop
(PIPELINE.md §10-11).

Same shape as `embedding_queue`/`entity_queue`, but the unit of work is a STORY, not an
article: one `agent.claims.extract()` call reads a story's whole evidence packet at
once, matching how PIPELINE.md §12's evidence packet is assembled. A job's `items` list
still batches multiple units per job the same way embed/entity do, but here each item
triggers its own separate model call inside the handler -- batching bounds how many jobs
beat creates per tick, not how much gets combined into one completion the way it does
for embeddings.

Selection is `claims_extracted_at IS NULL`, mirroring `entities_extracted_at`'s role:
the marker is set on every attempt, success or failure, so a story is not re-dispatched
forever just because its result was empty or its extraction failed. Unlike
`unscored_story_ids`, this also requires the story to be past its clustering join
window -- see `unclaimed_story_ids`'s docstring for why that gate exists here and not
for scoring.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from thedrop_database.dispatch import new_batch_key, outstanding_article_ids
from thedrop_database.models import Job, RawArticle, Source, Story, StorySource

logger = logging.getLogger(__name__)

JOB_TYPE = "extract_claims"

#: Per-article cap. Generous relative to entity_queue's 4000: extraction reads the full
#: body across potentially several articles in one call, not a compact tagging window,
#: and the desktop's context window has been verified (agent/claims.py) to hold well
#: past what a real multi-article story needs.
MAX_TEXT_CHARS_PER_ARTICLE = 8000


def _text_for(article: RawArticle) -> str:
    parts = [article.title or "", article.dek or "", article.body_text or ""]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())[:MAX_TEXT_CHARS_PER_ARTICLE]


def unclaimed_story_ids(db: Session, *, window_hours: int, limit: int) -> list[int]:
    """Unmerged stories, past their clustering join window, never attempted -- oldest
    first.

    The window_hours gate is not needed by `unscored_story_ids` (scoring.py) because
    re-running the score formula is cheap and side-effect-free. Extraction is a real
    model call with no automatic re-trigger when a story later gains a member, so
    dispatching before a story is done accumulating members would bake an incomplete
    evidence packet in permanently -- there is nothing downstream that notices a late
    joiner and asks for a re-extraction. A story past `cluster_join_threshold`'s own
    window (the same `window_hours` clustering.py uses to decide whether a new article
    may still join it) has stopped growing, or close enough that a genuine late joiner
    is rare and would need a manual re-extraction regardless of when this ran.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    return list(
        db.scalars(
            select(Story.id)
            .where(
                Story.merged_into_id.is_(None),
                Story.claims_extracted_at.is_(None),
                Story.last_activity_at <= cutoff,
            )
            .order_by(Story.first_seen_at, Story.id)
            .limit(limit)
        ).all()
    )


def _articles_for_story(db: Session, story_id: int) -> list[dict[str, str]]:
    rows = db.execute(
        select(RawArticle, Source.domain)
        .join(Source, Source.id == RawArticle.source_id)
        .join(StorySource, StorySource.raw_article_id == RawArticle.id)
        .where(StorySource.story_id == story_id)
        .order_by(RawArticle.published_at)
    ).all()

    articles: list[dict[str, str]] = []
    for article, domain in rows:
        text = _text_for(article)
        if not text:
            # Nothing to extract from. Left out of this story's evidence packet
            # rather than blocking the whole story -- the other members still carry
            # something to extract, the same reasoning entity_queue applies per
            # article rather than per batch.
            continue
        articles.append({"id": str(article.public_id), "source": domain, "text": text})
    return articles


def enqueue_extraction_batches(
    db: Session,
    *,
    window_hours: int,
    stories_per_batch: int = 5,
    max_batches: int = 4,
    priority: int = 0,
) -> list[str]:
    """Queue up to `max_batches` claim-extraction jobs, `stories_per_batch` stories
    each. Returns the idempotency keys queued.
    """
    story_ids = unclaimed_story_ids(
        db, window_hours=window_hours, limit=stories_per_batch * max_batches
    )
    if not story_ids:
        return []

    public_ids = dict(
        db.execute(select(Story.id, Story.public_id).where(Story.id.in_(story_ids))).all()
    )

    # Stories already inside a queued or leased job of this type. Excluded here rather
    # than deduplicated by a content-hashed key, same reasoning as embedding/entity
    # dispatch: a completed job's key must not block a legitimate re-dispatch forever.
    outstanding = outstanding_article_ids(db, JOB_TYPE)
    story_ids = [sid for sid in story_ids if str(public_ids[sid]) not in outstanding]

    queued: list[str] = []
    for start in range(0, len(story_ids), stories_per_batch):
        chunk = story_ids[start : start + stories_per_batch]
        items: list[dict[str, Any]] = []
        for story_id in chunk:
            articles = _articles_for_story(db, story_id)
            if not articles:
                # Nothing extractable from any member. Left for the ingestion defect
                # it is rather than marked extracted, the same convention entity_queue
                # uses when an article has no usable text.
                continue
            items.append({"id": str(public_ids[story_id]), "articles": articles})

        if not items:
            continue

        key = new_batch_key("claims-v1")
        result = db.execute(
            pg_insert(Job)
            .values(
                job_type=JOB_TYPE,
                payload={"items": items},
                priority=priority,
                idempotency_key=key,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Job.id)
        )
        if result.scalar() is not None:
            queued.append(key)

    if queued:
        logger.info("queued claim extraction batches", extra={"count": len(queued)})
    return queued
