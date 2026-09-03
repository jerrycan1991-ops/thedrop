"""US relevance scoring dispatch (PIPELINE.md 7).

Runs ON THE VPS, inline -- like clustering (ADR-0015), this needs the database and
needs no model. The two implemented signals (entities, publisher share) are pure SQL;
see thedrop_database.scoring for what is and is not covered.
"""

from __future__ import annotations

import logging

from thedrop_database import session_scope
from thedrop_database.scoring import unscored_story_ids, update_us_relevance

from app.celery_app import celery_app
from app.locks import dispatch_lock

logger = logging.getLogger(__name__)

#: Bounds a cold start the same way clustering's per-tick limit does: a large backlog
#: of newly-unmerged or newly-created stories must not make one tick do unbounded work.
MAX_PER_TICK = 200


@celery_app.task(name="app.tasks.score.score_us_relevance_batch")
def score_us_relevance_batch() -> dict[str, object]:
    """Score every story that does not have a us_relevance_score yet.

    Under a lock, same reasoning as clustering and consolidation: two overlapping
    ticks scoring the same story twice would waste work, not corrupt anything (scoring
    is idempotent -- the second write just repeats the first), but the lock is cheap
    and consistent with the other dispatchers.
    """
    with dispatch_lock("scoring") as acquired:
        if not acquired:
            return {"scored": 0, "status": "already_scoring"}

        with session_scope() as db:
            story_ids = unscored_story_ids(db, limit=MAX_PER_TICK)
            for story_id in story_ids:
                update_us_relevance(db, story_id)

    if story_ids:
        logger.info("scored stories", extra={"count": len(story_ids)})
    return {"scored": len(story_ids)}
