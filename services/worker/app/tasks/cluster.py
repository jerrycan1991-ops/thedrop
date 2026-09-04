"""Story clustering dispatch.

Runs ON THE VPS, inline — not as a desktop job (ADR-0015). Step 1 of the algorithm is
`ORDER BY centroid <=> $1`, a pgvector query, and the desktop holds no database
credentials. Nothing in incremental clustering needs a model; everything in it needs the
database.

The work itself lives in `thedrop_database.clustering`, which knows nothing about
Celery, so it is testable without a broker and reusable from a backfill script.
"""

from __future__ import annotations

import logging

from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.clustering import (
    cluster_pending,
    consolidate_stories,
    pending_clustering_count,
    rejoin_stragglers,
)

from app.celery_app import celery_app
from app.locks import dispatch_lock

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.cluster.cluster_ready_articles")
def cluster_ready_articles() -> dict[str, object]:
    """Cluster every article that is embedded and extracted.

    Under a lock: two overlapping ticks would each read the same unclustered articles
    and could found two stories for one event, which is the failure clustering exists to
    avoid rather than cause.
    """
    settings = get_settings()

    with dispatch_lock("clustering") as acquired:
        if not acquired:
            return {"clustered": 0, "pending": None, "status": "already_clustering"}

        with session_scope() as db:
            decisions = cluster_pending(
                db,
                limit=settings.ai.cluster_max_per_tick,
                join_threshold=settings.ai.cluster_join_threshold,
                window_hours=settings.ai.cluster_window_hours,
                candidate_limit=settings.ai.cluster_candidate_limit,
                max_fraction=settings.ai.entity_guard_max_doc_fraction,
                min_floor=settings.ai.entity_guard_min_doc_floor,
            )
            # Measured AFTER the work, so it reports what is still waiting rather than
            # what was waiting when the task started. The first real run said
            # `clustered: 152, pending: 152`, which reads as though nothing moved.
            pending = pending_clustering_count(db)

    joined = sum(1 for d in decisions if d.joined)
    if decisions:
        logger.info(
            "clustered", extra={"articles": len(decisions), "joined": joined, "pending": pending}
        )
    return {
        "clustered": len(decisions),
        "joined": joined,
        "new_stories": len(decisions) - joined,
        "pending": pending,
    }


@celery_app.task(name="app.tasks.cluster.consolidate_recent_stories")
def consolidate_recent_stories() -> dict[str, object]:
    """Merge stories that are the same event, then reunite any singleton straggler
    with a larger story it should have joined at the ORIGINAL join threshold.

    The counterweight to a design that deliberately over-splits: join-or-create refuses
    whenever it is unsure and the digest rule refuses again, and nothing else puts the
    duplicates back together. `consolidate_stories` catches near-duplicates at its own
    (stricter) threshold; `rejoin_stragglers` catches the gap consolidation cannot --
    see its docstring for why that gap exists and why the fix is deliberately narrow
    rather than a lower threshold applied everywhere.

    Runs less often than clustering. A merge is a bigger claim than a join, and there is
    nothing to consolidate until clustering has produced duplicates to consolidate.
    """
    settings = get_settings()

    with dispatch_lock("consolidation") as acquired:
        if not acquired:
            return {"merges": 0, "rejoins": 0, "status": "already_consolidating"}

        with session_scope() as db:
            merges = consolidate_stories(
                db,
                window_hours=settings.ai.cluster_window_hours,
                merge_threshold=settings.ai.cluster_merge_threshold,
                max_merges=settings.ai.cluster_max_merges_per_pass,
                max_fraction=settings.ai.entity_guard_max_doc_fraction,
                min_floor=settings.ai.entity_guard_min_doc_floor,
            )
            # After consolidation, not before: a merge can turn a singleton straggler's
            # target into a larger story, or turn what WAS the target into a singleton
            # itself (absorbed by something else) -- running this second reads the
            # state consolidation actually left behind.
            rejoins = rejoin_stragglers(
                db,
                window_hours=settings.ai.cluster_window_hours,
                join_threshold=settings.ai.cluster_join_threshold,
                max_rejoins=settings.ai.cluster_max_merges_per_pass,
                max_fraction=settings.ai.entity_guard_max_doc_fraction,
                min_floor=settings.ai.entity_guard_min_doc_floor,
            )

    if merges or rejoins:
        logger.info("consolidated", extra={"merges": len(merges), "rejoins": len(rejoins)})
    return {"merges": len(merges), "rejoins": len(rejoins)}
