"""Publication tasks (Phase 4).

Promotes approved articles to live, revalidates ISR paths, regenerates sitemaps and
enqueues distribution.

The publishing *gate* itself is not here -- it runs in the API, in Python, reading the
database, so that no model output can raise its own confidence or move a threshold
(SECURITY.md §6.3).
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.publish.process_queue")
def process_queue() -> dict[str, object]:
    """Placeholder. Implemented in Phase 4."""
    return {"implemented": False, "phase": 4}
