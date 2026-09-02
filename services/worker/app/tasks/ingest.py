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
from thedrop_ingest.pipeline import due_providers, poll

from app.celery_app import celery_app
from app.locks import provider_lock

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ingest.poll_provider")
def poll_provider(provider_slug: str) -> dict[str, object]:
    """Poll one provider. Never raises for a provider-side fault.

    A failing feed updates its circuit breaker and reports; it must not take down the
    beat task that polls every other provider.
    """
    with provider_lock(provider_slug) as acquired:
        if not acquired:
            # A previous poll is still running. Skipping is correct: the next tick will
            # try again, and fetching the same feed twice concurrently would double the
            # request rate at a publisher who never agreed to it.
            return {"provider": provider_slug, "status": "already_polling"}

        with session_scope() as db:
            return poll(db, provider_slug)


@celery_app.task(name="app.tasks.ingest.dispatch_due_providers")
def dispatch_due_providers() -> dict[str, object]:
    """Fan out `poll_provider` for every enabled provider past its interval.

    Runs on beat every 60 seconds. Beat itself stays a fixed schedule and the DECISION
    about what is due lives in a query, so changing a provider's cadence is a row
    update rather than a redeploy -- and the logic is testable without a broker.
    """
    with session_scope() as db:
        slugs = due_providers(db)

    for slug in slugs:
        poll_provider.delay(slug)

    if slugs:
        logger.info("dispatched providers", extra={"count": len(slugs), "slugs": slugs})
    return {"dispatched": slugs, "count": len(slugs)}
