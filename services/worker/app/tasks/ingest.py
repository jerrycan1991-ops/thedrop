"""Ingestion tasks (Phase 2).

Provider polling, normalization and cheap deduplication live here. Deliberately empty
in Phase 1 -- the module exists so the queue routing and the Celery ``include`` list
are correct from the start, and so Phase 2 is additive rather than structural.

Cheap dedup (canonical URL hash, content hash, SimHash) runs here on the VPS.
Embeddings and clustering do NOT -- those are desktop jobs (ADR-0005).
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ingest.poll_provider")
def poll_provider(provider_slug: str) -> dict[str, object]:
    """Placeholder. Implemented in Phase 2."""
    logger.info("ingest not yet implemented", extra={"provider": provider_slug})
    return {"provider": provider_slug, "implemented": False, "phase": 2}
