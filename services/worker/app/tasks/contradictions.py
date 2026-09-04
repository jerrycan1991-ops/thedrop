"""Contradiction-check dispatch (PIPELINE.md §11).

Thin wrapper, same shape as claims.py: the decision about which stories are ready
lives in `thedrop_database.contradiction_queue`, which knows nothing about Celery.

The VPS runs no model (CLAUDE.md resource discipline). This queues work and stops. If
the desktop is offline the batches wait -- see ADR-0001.

No AI_ENABLED gate here, matching claims.py: the ollama path this stage currently uses
(ADR-0020, ADR-0023) is local, free GPU work, not paid API usage, so the gate that
exists to bound cost risk does not apply to it.
"""

from __future__ import annotations

import logging

from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.contradiction_queue import enqueue_contradiction_batches

from app.celery_app import celery_app
from app.locks import dispatch_lock

logger = logging.getLogger(__name__)

#: Stories per job. Same value and same reasoning as claims.py's STORIES_PER_JOB: a
#: bad batch has a small, predictable blast radius regardless of how the tick-level
#: cap is tuned.
STORIES_PER_JOB = 5


@celery_app.task(name="app.tasks.contradictions.dispatch_contradiction_check_batches")
def dispatch_contradiction_check_batches() -> dict[str, object]:
    """Queue contradiction-check jobs for stories with extracted claims that have
    never been through this stage."""
    settings = get_settings()
    max_stories = settings.ai.contradiction_check_max_stories_per_tick

    with dispatch_lock("contradictions") as acquired:
        if not acquired:
            return {"queued": 0, "status": "already_dispatching"}

        with session_scope() as db:
            queued = enqueue_contradiction_batches(
                db,
                stories_per_batch=STORIES_PER_JOB,
                max_batches=-(-max_stories // STORIES_PER_JOB),  # ceil division
            )

    if queued:
        logger.info("queued contradiction-check batches", extra={"batches": len(queued)})
    return {"queued": len(queued)}
