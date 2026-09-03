# ADR-0019: `StoryEntity` is resynced whenever a member article is re-extracted

Status: Accepted (Phase 3)

Date: 2026-09-04

## Context

`StoryEntity` is a story's promoted/cached entity set. It is read by two consumers:
`thedrop_database.scoring._entity_signal` (US relevance, cosmetic if wrong) and
`thedrop_database.clustering.story_guard_entities` / `shared_guard_entities` (the
LIVE clustering join decision — not cosmetic if wrong).

Until now it was written exactly once, by `_promote_entities`, at the moment an
article joins a story. Nothing kept it in sync afterward.

This surfaced as an anomaly in the deployed US-relevance score: a Nepal-floods story
scored higher than expected, and its `us_relevance_basis` showed "United States" and
"Alaska" among its matched entities. Neither was in the story's eight current member
articles' `raw_article_entities`. Tracing it back: one of those eight articles had been
re-extracted during an earlier entity-normalisation backfill (see ADR-0017's alias-leak
fix), and that re-extraction no longer produced "United States" — but the story's
`StoryEntity` snapshot still had the row from the original extraction, because nothing
had ever told it to update.

`store_entities` (`services/api/app/routers/worker.py`) replaces a re-extracted
article's `raw_article_entities` wholesale, but had never touched the story it belongs
to. The gap was not "a promotion step is missing" — `_promote_entities` is correct for
what it does, additive at join time. The gap was that nothing existed for the case a
member article's entities *change after* the story already exists.

## Decision

Added `resync_story_entities(db, story_id)`
(`packages/database/src/thedrop_database/clustering.py`): a full DELETE-then-rebuild of
a story's `StoryEntity` set from the union of all current members' current
`raw_article_entities`, not a diff. The failure mode being fixed is exactly "a stale
row nobody noticed"; a diff-based patch leaves the same class of bug possible for
whatever the diff logic doesn't account for.

`store_entities` now collects the set of story ids touched by a batch of
re-extractions and calls `resync_story_entities` once per affected story, right before
`db.commit()`.

**Repair, not just prevention.** Stories that went stale before this fix existed do not
self-heal — the API only resyncs on the next re-extraction, and most stories will never
be re-extracted again. `infrastructure/scripts/resync_all_story_entities.py` walks
every non-merged story and resyncs it unconditionally. Unconditional, not
targeted-by-heuristic: the recompute is a cheap, pure SQL aggregation per story, so
"resync everything" is simpler and safer than trying to identify which stories were
affected (e.g. via `entities_extracted_at` vs. `StorySource.added_at` timestamps, which
would need its own correctness argument and could itself miss cases).

## Consequences

- `story_guard_entities` / `shared_guard_entities` can no longer be licensed by a ghost
  entity left over from a prior extraction. This directly affects which future articles
  are allowed to join a story — the scoring anomaly was the symptom that surfaced it,
  not the actual risk.
- Every re-extraction now does one extra story-scoped aggregation query when the
  article already belongs to a story. Negligible: re-extraction is not a hot path, and
  the aggregation is grouped by `entity_id` over a single story's members.
- `resync_all_story_entities.py` is a one-time repair for data that predates this fix.
  It is not scheduled and does not need to be — going forward, `store_entities` keeps
  `StoryEntity` correct on every re-extraction, so nothing should go stale again absent
  a new code path that mutates `raw_article_entities` without going through
  `store_entities`.
- If a future code path adds or removes entities on an already-clustered article
  outside `store_entities`, it must call `resync_story_entities` itself or reintroduce
  this exact bug.
