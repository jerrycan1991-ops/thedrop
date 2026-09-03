"""Embedding dispatch.

Thin wrapper, same shape as ingest: the decision about what needs embedding lives in
`thedrop_database.embedding_queue`, which takes a session and knows nothing about
Celery, so it is testable without a broker.

The VPS runs no model (ADR-0005). This queues work and stops. If the desktop is
offline the batches simply wait, which is the designed behaviour -- the public site
does not depend on the desktop being up.
"""

from __future__ import annotations

import logging

from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.embedding_queue import (
    enqueue_embedding_batches,
    pending_embedding_count,
)

from app.celery_app import celery_app
from app.locks import dispatch_lock

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.embed.dispatch_embedding_batches")
def dispatch_embedding_batches() -> dict[str, object]:
    """Queue embedding jobs for articles that have none.

    Articles already inside a queued or leased job are excluded by the queue itself, so
    a tick arriving while the previous batch is still in flight adds nothing rather than
    duplicating it.
    """
    settings = get_settings()

    with dispatch_lock("embeddings") as acquired:
        if not acquired:
            # A previous tick is still dispatching. Skipping is correct: the next tick
            # sees whatever it queued and carries on from there.
            return {"queued": 0, "pending": None, "status": "already_dispatching"}

        with session_scope() as db:
            pending = pending_embedding_count(db)
            queued = enqueue_embedding_batches(
                db,
                batch_size=settings.ai.embedding_batch_size,
                max_batches=settings.ai.embedding_max_batches_per_tick,
            )

    if queued:
        logger.info("queued embedding batches", extra={"batches": len(queued), "pending": pending})
    return {"queued": len(queued), "pending": pending}
