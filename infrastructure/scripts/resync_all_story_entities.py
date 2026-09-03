"""One-time repair: resync every active story's `StoryEntity` set from its current
members' current entities.

    uv run python infrastructure/scripts/resync_all_story_entities.py --dry-run
    uv run python infrastructure/scripts/resync_all_story_entities.py

Found via a US-relevance-score anomaly on a Nepal-floods story: `_promote_entities`
writes `StoryEntity` once, at join time, and nothing kept it in sync afterward. An
article that had already joined a story, then got re-extracted (as today's earlier
entity-normalisation backfill did), left the story holding ghost entities no current
member actually carries. `store_entities` (services/api/app/routers/worker.py) now
calls `thedrop_database.clustering.resync_story_entities` for any story it re-extracts
an article of, so this cannot recur going forward -- see that function's docstring.

This script is the other half: repairing stories that already went stale before the
fix existed. It resyncs unconditionally rather than trying to detect which stories were
affected, because the recompute is a cheap, pure SQL aggregation and doing it for every
story is simpler and safer than a heuristic for "which ones might be wrong" -- the same
reasoning `resync_story_entities` itself uses for DELETE-then-rebuild over a diff.

Idempotent: a story whose StoryEntity set already matches its members reports no
change and touches no row.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import session_scope
from thedrop_database.clustering import resync_story_entities
from thedrop_database.models import RawArticleEntity, Story, StoryEntity, StorySource
from thedrop_database.operator_env import load_operator_env


def _current_entity_ids(db: Session, story_id: int) -> set[int]:
    return set(
        db.scalars(
            select(RawArticleEntity.entity_id)
            .join(StorySource, StorySource.raw_article_id == RawArticleEntity.raw_article_id)
            .where(StorySource.story_id == story_id)
            .group_by(RawArticleEntity.entity_id)
        ).all()
    )


def _stored_entity_ids(db: Session, story_id: int) -> set[int]:
    return set(
        db.scalars(select(StoryEntity.entity_id).where(StoryEntity.story_id == story_id)).all()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change; apply nothing"
    )
    args = parser.parse_args()

    loaded = load_operator_env()
    if loaded:
        print(f"(configuration from {loaded})\n")

    changed = 0
    unchanged = 0
    with session_scope() as db:
        # merged_into_id IS NULL: an absorbed story's members moved to the survivor,
        # which is itself in this set -- resyncing the absorbed row would just compute
        # against members it no longer has.
        story_ids = db.scalars(
            select(Story.id).where(Story.merged_into_id.is_(None)).order_by(Story.id)
        ).all()

        for story_id in story_ids:
            before = _stored_entity_ids(db, story_id)
            after = _current_entity_ids(db, story_id)

            if before == after:
                unchanged += 1
                continue

            added = after - before
            removed = before - after
            action = "would resync" if args.dry_run else "resync"
            print(f"  {action:<12} story {story_id}: +{len(added)} entity, -{len(removed)} entity")
            changed += 1
            if not args.dry_run:
                resync_story_entities(db, story_id)

    print(
        f"\n{'would change' if args.dry_run else 'changed'} {changed} stories, "
        f"{unchanged} already correct, {len(story_ids)} checked"
    )
    if args.dry_run:
        print("(dry run -- nothing was changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
