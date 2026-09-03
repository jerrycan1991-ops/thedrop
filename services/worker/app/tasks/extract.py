"""Entity extraction dispatch.

Thin wrapper, same shape as ingest and embed: the decision about what needs extracting
lives in `thedrop_database.entity_queue`, which knows nothing about Celery.

The VPS runs no model (CLAUDE.md resource discipline). This queues work and stops. If
the desktop is offline the batches wait, and clustering under-splits rather than
merging wrongly -- see ADR-0015.
"""

from __future__ import annotations

import logging

from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.entity_queue import (
    enqueue_extraction_batches,
    pending_extraction_count,
)

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.extract.dispatch_extraction_batches")
def dispatch_extraction_batches() -> dict[str, object]:
    """Queue entity-extraction jobs for articles that have never been through it."""
    settings = get_settings()

    with session_scope() as db:
        pending = pending_extraction_count(db)
        queued = enqueue_extraction_batches(
            db,
            batch_size=settings.ai.entity_batch_size,
            max_batches=settings.ai.entity_max_batches_per_tick,
        )

    if queued:
        logger.info("queued extraction batches", extra={"batches": len(queued), "pending": pending})
    return {"queued": len(queued), "pending": pending}
