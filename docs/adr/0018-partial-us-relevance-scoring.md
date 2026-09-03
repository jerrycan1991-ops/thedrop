# ADR-0018: US relevance scored on 2 of 5 signals, rescaled and labelled as partial

Status: Accepted (Phase 3)

Date: 2026-09-04

## Context

PIPELINE.md §7 specifies US relevance as five weighted signals:

| Signal | Weight |
|---|---|
| US entities | 0.30 |
| US publisher share | 0.20 |
| Topic class US-salience | 0.20 |
| Direct impact on US audiences | 0.20 |
| US search/trend signal presence | 0.10 |

Two are mechanically computable from data already in the database: entity names
against a curated marker list, and the country of each member article's source. The
other three are not buildable honestly in this step:

- **Search/trend signal** needs an external API (Google Trends or similar) with no
  integration in this project. Inventing a value would violate CLAUDE.md's "never
  fabricate" rule directly.
- **Topic class US-salience** and **direct impact on US audiences** are judgements
  about story content — "is this domestic policy," "does this affect prices or
  safety" — not things a structured query can honestly determine. A hand-built
  keyword heuristic standing in for these would be a fabricated signal wearing the
  clothes of a computed one: it would look like data and would not be.

## Options considered

1. **Wait until all five signals exist.** Correct in the abstract, but blocks a real,
   useful partial signal behind work (an LLM classifier, an external API integration)
   that is substantially larger than "the next step" implied, and that nothing
   downstream currently consumes anyway — Phase 4 (article generation) does not exist
   yet, so there is no gate this blocks today.
2. **Implement the two computable signals, leave the score capped near 50.** Honest,
   but numerically useless: an unmistakably American story and a barely-American one
   would both land in the same "middling" band for a reason no viewer of the score
   could see, since nothing distinguishes "half the formula says 100" from "the whole
   formula says 50."
3. **Implement the two computable signals, rescale them to fill 0–100, and record
   what fraction of the real formula they represent.**

## Decision

Option 3. `WEIGHT_ENTITIES` (0.30) and `WEIGHT_PUBLISHER_SHARE` (0.20) are rescaled
against their own sum (0.60 and 0.40 respectively) so the stored score genuinely uses
the full 0–100 range based on what is actually measured, rather than being capped by
signals that were never evaluated.

`stories.us_relevance_basis` (JSONB) is what keeps this honest: it records
`coverage: 0.50`, the raw value of each signal that ran, and an explicit
`signals_not_implemented` list. This is the same role `sources.reliability_basis`
already plays for `reliability_score` — provenance for a number that will otherwise be
read as more complete than it is.

**Not wired to any gate.** PIPELINE.md ties `US_RELEVANCE_MIN` to whether a story gets
written, but nothing writes stories yet. Building a gate with no consumer would be
exactly the premature abstraction CLAUDE.md's engineering discipline warns against.
Whoever builds Phase 4 decides how a partial-coverage score should be treated at the
gate — most plausibly, requiring `coverage` above some threshold before trusting the
score for that decision at all.

**Runs on the VPS**, inline, following ADR-0015's reasoning for clustering: this needs
a database and needs no model. CLAUDE.md's resource discipline is about ML runtimes,
not about SQL — a scoring stage that happens to be entirely SQL does not belong on the
desktop merely because PIPELINE.md's original stage table said "DESKTOP" for the whole
of stage 7, written before this two-signal split existed.

## A defect found while building this

`sources.country` already existed, defaulting to `"US"`, and **nothing had ever set
it to anything else** — every source in production, including `theguardian.com`
(genuinely UK-headquartered), was recorded as US. Left uncorrected, the publisher-share
signal would have been silently wrong for every non-US source in the corpus from the
day this shipped.

Fixed with the same pattern as `_AUTHORITY_SUFFIXES`: a small, explicit,
publicly-verifiable override map (`_NON_US_DOMAINS`) consulted in `resolve_source` when
a source is first auto-created, plus a one-time correction script
(`correct_source_countries.py`) for sources created before the override existed. The
override is derived from real, checkable facts about real organisations — the same
epistemic status as the existing authority-suffix list — not a fabrication.

## Consequences

- A story's `us_relevance_score` today measures roughly "does this involve identifiably
  American places/institutions, and is it being covered by US outlets" — a real and
  useful signal, but a narrower question than "is this relevant to a US audience."
  Anyone reading the score without reading `us_relevance_basis` first will overestimate
  what it represents.
- `US_ENTITY_MARKERS` is a small, curated list (50 states + DC + the country itself +
  a short list of unambiguous federal institutions), not a gazetteer. A story about a
  US topic that happens to name none of these scores 0 on this signal — a true
  statement about what was found, not a claim the story is not American. This will
  under-score some genuinely American stories until the list is extended or a better
  classifier replaces this signal.
- The three unimplemented signals are real, separate future work: an LLM-based
  classifier for topic class and direct impact (following ADR-0008's untrusted-content
  handling, the same pattern the entity/claim extraction stage will need), and a
  decision about which external trend API to integrate, if any.
- `_NON_US_DOMAINS` needs an entry every time a genuinely non-US feed is added, or that
  feed silently inherits the "US" default the same way `theguardian.com` did. There is
  no automated check for this; it depends on whoever adds a feed remembering.
