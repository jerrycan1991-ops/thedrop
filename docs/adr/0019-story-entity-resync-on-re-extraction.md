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

This was first suspected from an anomaly in the deployed US-relevance score: a
Nepal-floods story (id 80) scored higher than expected, and its `us_relevance_basis`
showed "United States" and "Alaska" among its matched entities. A diagnostic query at
the time found only "Alaska" among the story's current member articles'
`raw_article_entities` — "United States" appeared absent — which read as exactly the
symptom a stale `StoryEntity` row would produce, and matched a real re-extraction that
had run earlier the same day (an entity-normalisation backfill, see ADR-0017's
alias-leak fix).

That specific diagnosis does not hold up under direct re-verification (see
"Correction" below): story 80's `StoryEntity` and its members' current entities turned
out to match exactly, with "United States" genuinely present in both. The likely
explanation is mundane — one member article had probably not yet finished its first
extraction when the original query ran, and completed normally afterward. `_promote_entities`
had not yet run on stale data for this story; a pipeline snapshot mid-flight was
misread as a stale cache.

The architectural gap itself is real regardless of story 80's specific history:
`store_entities` (`services/api/app/routers/worker.py`) replaces a re-extracted
article's `raw_article_entities` wholesale, but had never touched the story it belongs
to. The gap was not "a promotion step is missing" — `_promote_entities` is correct for
what it does, additive at join time. The gap was that nothing existed for the case a
member article's entities *change after* the story already exists — reproduced
directly by construction in `tests/test_entity_extraction_db.py` (build a story, plant
a `StoryEntity` row backed by no current article, re-extract, assert the ghost is
gone), independent of whether story 80 ever actually hit this path.

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

## Correction (post-deploy verification)

After deploying the fix, `resync_all_story_entities.py --dry-run` was run against
production: **0 of 398 live stories needed any change**, story 80 included. Direct
comparison of story 80's `StoryEntity` against its members' current
`raw_article_entities` showed the two sets already identical, "United States" and
"Alaska" both genuinely present — a real article in that story compares Nepal's flood
to glacial-outburst-flood risk in the US Pacific Northwest (Mount Rainier, USGS,
University of Calgary all appear among its entities). The score of 45 is correct, not
an anomaly: 100% US publisher share (8/8 sources) plus a small genuine entity signal.

Read plainly, this means the bug this ADR fixes was real and is now demonstrated by
direct construction, but does not appear to have actually corrupted any story in
production before the fix shipped — a latent defect closed before it caused visible
harm, not one caught after the fact. The value of the fix is unchanged: it removes a
live risk to `story_guard_entities`'s correctness going forward. What changed is the
origin story — this was not "found because story 80 was already wrong," it was "found
because story 80's number looked surprising, which led to discovering a real gap that,
on inspection, story 80 itself had not fallen into."

## Consequences

- `story_guard_entities` / `shared_guard_entities` can no longer be licensed by a ghost
  entity left over from a prior extraction. This directly affects which future articles
  are allowed to join a story, regardless of whether any live story had hit the gap yet.
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
