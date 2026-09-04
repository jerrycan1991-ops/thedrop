# ADR-0021: reunite singleton stragglers at the join threshold, not the merge threshold

Status: Accepted (Phase 3)

Date: 2026-09-04

## Context

`label_recall.py --missed` surfaced several `same_event` pairs blocked neither by
similarity threshold nor by the entity guard -- the two conditions `blocker()` checks
before falling back to `"other"`. Its own comment names two candidate causes: the 48h
clustering window, or the digest rule.

Tracing three real production pairs (a Lindsay Clancy trial story split across two
story rows, two Iran-strikes stories, a Pentagon/Hegseth-turmoil pair) against their
actual article timestamps ruled out the window explanation directly: every article in
every pair landed within a day or two of the others, nowhere near the 48h cutoff. The
Hegseth/Pentagon pair's first articles were discovered at the exact same microsecond
(`2026-09-02 12:43:30.012683`), a strong sign both were created as new stories in the
same processing batch, each unable to see the other as a candidate yet. The Lindsay
Clancy pair's smaller side never grew past its founding article while the larger side
kept absorbing new members for two more days, consistent with the digest rule
correctly declining to join a story-spanning article and leaving it stranded.

Neither cause matters as much as what they have in common: **once split, nothing puts
these back together.** `consolidate_stories` is the only mechanism that merges
existing stories, and its threshold (`cluster_merge_threshold`, 0.90) is deliberately
higher than the live join threshold (`cluster_join_threshold`, 0.82) -- ADR-0015's own
reasoning is that merging asserts everything already in BOTH stories is one event, a
stronger claim than a single join makes. All three real pairs scored between 0.729 and
0.843: high enough that a fresh article at that similarity would join an existing
story, too low to ever be merged back together after the fact. The same evidence
supports opposite decisions depending only on which direction it runs.

## Decision

`thedrop_database.clustering.rejoin_stragglers`, run immediately after
`consolidate_stories` in the same `consolidate_recent_stories` Celery task. It reuses
`join_threshold` (0.82), not `merge_threshold` (0.90), and the same
`story_guard_entities` intersection every other join or merge decision uses.

Deliberately narrower than lowering `merge_threshold` everywhere, per explicit
operator choice among three options (lower the threshold globally, add a narrow
targeted pass, or just document the gap):

- **only a story with exactly one member article is a rejoin candidate.** A story that
  has already attracted a second, independent article has demonstrated it is a real
  cluster, not an under-joined straggler -- lowering the bar for that case is exactly
  what `merge_threshold`'s higher bar exists to prevent. Article count, not
  `source_count`: two articles from the same outlet already show the story is not a
  bare one-off, and `source_count` (distinct publishers) would not see that, since a
  same-outlet follow-up leaves it at 1.
- **a straggler may only join a strictly LARGER story.** "The straggler finishes the
  join it missed", not two arbitrary stories merging into whichever happens to be
  older, which is `consolidate_stories`' rule. The larger story survives here, the
  reverse of that rule, because a singleton rejoining an established story is exactly
  what a live join would have produced.
- **two singletons never rejoin each other**, even an obvious duplicate pair (the
  production Iran-strikes example: two singleton stories, both headlined "Iran fires
  on its Gulf neighbors, retaliating for U.S. strikes", discovered under four hours
  apart, never compared to each other). Neither is "larger" than the other, so neither
  is a valid target under this function's own rule. Left to `consolidate_stories`, at
  its higher bar, deliberately -- not widened into this pass.

## Consequences

- Closes the 0.82-0.90 dead zone for exactly the case it was built for: a singleton
  that should have joined an established story. It does not close every version of the
  underlying problem -- two singletons that are the same event, or two stories that
  BOTH have multiple members, still have no path back together below 0.90. Those
  remain `consolidate_stories`' job, or a future, differently-scoped pass, not this one.
- Runs in the same task and under the same `dispatch_lock("consolidation")` as
  `consolidate_stories`, after it rather than before: a consolidation merge can turn a
  straggler's best target into a larger story, or turn what was a target into a
  singleton itself (absorbed by something bigger). Running rejoin second means it
  reads the state consolidation actually left behind rather than a stale snapshot.
- Uses the same `merge_stories` mechanics consolidation already uses (the absorbed row
  is kept with `merged_into_id` set, never deleted), so a straggler rejoin is exactly
  as auditable and exactly as reversible-in-principle as any other merge in this
  system -- no new risk category, just a new place the existing mechanism runs from.
- No dry-run mode, matching `consolidate_stories`' own precedent: trusted via its test
  suite and the beat-smoke deploy gate, not a separate manual-approval step. If this
  turns out to be too permissive in practice, the fix is the same lever every other
  clustering decision in this codebase already has -- the guard and the threshold.
