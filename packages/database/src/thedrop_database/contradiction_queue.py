"""Turn stories ready for contradiction-checking into work orders for the desktop
(PIPELINE.md §11).

Same shape as `claim_queue`: the unit of work is a STORY, one job item per story, one
`agent.contradictions.find_contradictions()` call inside the handler per item.

Selection is `claims_extracted_at IS NOT NULL` (extraction has run) AND
`contradictions_checked_at IS NULL` (never attempted, mirroring how
`claims_extracted_at` itself gates claim-extraction dispatch) AND at least two claims
exist. The last condition is not optional the way it might look: `find_contradictions`
already short-circuits gracefully on fewer than two claims, but that is a courtesy for
a caller who did not know in advance -- this dispatcher DOES know, from a query it is
already running, so sending that job anyway would just be a round trip that always
returns empty.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from thedrop_database.dispatch import new_batch_key, outstanding_article_ids
from thedrop_database.models import Claim, Job, Story

logger = logging.getLogger(__name__)

JOB_TYPE = "find_contradictions"


def uncontested_story_ids(db: Session, *, limit: int) -> list[int]:
    """Stories with extracted claims, never checked for contradictions, oldest first.

    No window-hours gate the way `claim_queue.unclaimed_story_ids` needs one: a
    contradiction check reads claims that already exist, and re-running it later if
    the claim set changes is a correction, not a repeat of wasted work the way
    dispatching extraction too early would be.
    """
    return list(
        db.scalars(
            select(Story.id)
            .join(Claim, Claim.story_id == Story.id)
            .where(
                Story.merged_into_id.is_(None),
                Story.claims_extracted_at.is_not(None),
                Story.contradictions_checked_at.is_(None),
            )
            .group_by(Story.id, Story.first_seen_at)
            .having(func.count(Claim.id) >= 2)
            .order_by(Story.first_seen_at, Story.id)
            .limit(limit)
        ).all()
    )


def _claims_for_story(db: Session, story_id: int) -> list[dict[str, str]]:
    rows = db.execute(
        select(Claim.public_id, Claim.claim_text, Claim.claim_type, Claim.attributed_to_entity_id)
        .where(Claim.story_id == story_id)
        .order_by(Claim.id)
    ).all()
    # Resolve attribution names in one extra pass rather than a join per row: most
    # claims have no attribution at all, and the join would be null for all of them.
    entity_ids = {r.attributed_to_entity_id for r in rows if r.attributed_to_entity_id}
    names: dict[int, str] = {}
    if entity_ids:
        from thedrop_database.models import Entity

        names = dict(
            db.execute(
                select(Entity.id, Entity.canonical_name).where(Entity.id.in_(entity_ids))
            ).all()
        )

    return [
        {
            "id": str(r.public_id),
            "text": r.claim_text,
            "type": r.claim_type,
            "attributedTo": names.get(r.attributed_to_entity_id, "")
            if r.attributed_to_entity_id
            else "",
        }
        for r in rows
    ]


def enqueue_contradiction_batches(
    db: Session,
    *,
    stories_per_batch: int = 5,
    max_batches: int = 4,
    priority: int = 0,
) -> list[str]:
    """Queue up to `max_batches` contradiction-check jobs, `stories_per_batch`
    stories each. Returns the idempotency keys queued.
    """
    story_ids = uncontested_story_ids(db, limit=stories_per_batch * max_batches)
    if not story_ids:
        return []

    public_ids = dict(
        db.execute(select(Story.id, Story.public_id).where(Story.id.in_(story_ids))).all()
    )

    outstanding = outstanding_article_ids(db, JOB_TYPE)
    story_ids = [sid for sid in story_ids if str(public_ids[sid]) not in outstanding]

    queued: list[str] = []
    for start in range(0, len(story_ids), stories_per_batch):
        chunk = story_ids[start : start + stories_per_batch]
        items: list[dict[str, Any]] = []
        for story_id in chunk:
            claims = _claims_for_story(db, story_id)
            if len(claims) < 2:
                # Can happen between the dispatch query and here if claims changed
                # mid-tick; left for the next tick rather than sent as dead weight.
                continue
            items.append({"id": str(public_ids[story_id]), "claims": claims})

        if not items:
            continue

        key = new_batch_key("contradictions-v1")
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
        logger.info("queued contradiction-check batches", extra={"count": len(queued)})
    return queued
