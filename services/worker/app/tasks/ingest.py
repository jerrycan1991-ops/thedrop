"""Ingestion tasks.

Thin wrappers. The work lives in `thedrop_ingest.pipeline`, which takes a session and
knows nothing about Celery -- so it can be tested without a broker, and so a second
caller (a backfill script, the admin) can reuse it without going through the queue.

Cheap dedup (canonical URL hash, content hash, SimHash) runs here on the VPS.
Embeddings and clustering do NOT -- those are desktop jobs (ADR-0005).
"""

from __future__ import annotations

import logging

from thedrop_database import session_scope
from thedrop_ingest.pipeline import poll

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ingest.poll_provider")
def poll_provider(provider_slug: str) -> dict[str, object]:
    """Poll one provider. Never raises for a provider-side fault.

    A failing feed updates its circuit breaker and reports; it must not take down the
    beat task that polls every other provider.
    """
    with session_scope() as db:
        return poll(db, provider_slug)
