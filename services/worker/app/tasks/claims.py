"""Claim extraction dispatch (PIPELINE.md §10-11).

Thin wrapper, same shape as extract.py: the decision about which stories are ready
lives in `thedrop_database.claim_queue`, which knows nothing about Celery.

The VPS runs no model (CLAUDE.md resource discipline). This queues work and stops. If
the desktop is offline the batches wait -- see ADR-0001.

No AI_ENABLED gate here, matching embed.py/extract.py: those never check it either,
because it exists to gate PAID API usage risk, and this (like embedding and entity
extraction) is local, free GPU work while the "ollama" provider is what's configured
(ADR-0020). If the anthropic path is ever wired in for this stage, that path -- not
this dispatcher -- is where a cost gate belongs.
"""

from __future__ import annotations

import logging

from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.claim_queue import enqueue_extraction_batches

from app.celery_app import celery_app
from app.locks import dispatch_lock

logger = logging.getLogger(__name__)

#: Stories per job. Fixed rather than derived from the per-tick total, so a bad batch
#: has a small, predictable blast radius regardless of how the tick-level cap is
#: tuned -- same reasoning entity_queue's smaller-than-embedding batch size uses.
STORIES_PER_JOB = 5


@celery_app.task(name="app.tasks.claims.dispatch_claim_extraction_batches")
def dispatch_claim_extraction_batches() -> dict[str, object]:
    """Queue claim-extraction jobs for stories past their clustering join window that
    have never been through extraction."""
    settings = get_settings()
    max_stories = settings.ai.claim_extract_max_stories_per_tick

    with dispatch_lock("claims") as acquired:
        if not acquired:
            return {"queued": 0, "status": "already_dispatching"}

        with session_scope() as db:
            queued = enqueue_extraction_batches(
                db,
                window_hours=settings.ai.cluster_window_hours,
                stories_per_batch=STORIES_PER_JOB,
                max_batches=-(-max_stories // STORIES_PER_JOB),  # ceil division
            )

    if queued:
        logger.info("queued claim extraction batches", extra={"batches": len(queued)})
    return {"queued": len(queued)}
