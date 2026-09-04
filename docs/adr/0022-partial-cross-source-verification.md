# ADR-0022: cross-source verification ships 3 of 5 outcomes, deterministically

Status: Accepted (Phase 3)

Date: 2026-09-04

## Context

PIPELINE.md §11 specifies five verification outcomes per claim:

| Evidence | Resulting status |
|---|---|
| ≥ 2 independent credible sources agree | `corroborated` |
| A directly relevant authoritative primary source | `authoritative` |
| 1 source only | `single_source` |
| Sources conflict | `disputed` |
| Contradicted by an authoritative source | `refuted` |

Three of the five turn out to be mechanically computable from data this pipeline
already produces, with no model call:

- **`authoritative`** — `sources.is_primary_authority` already exists and is already
  populated at ingestion (`thedrop_ingest.pipeline.resolve_source`, for
  `.gov`/`.mil`/`.gov.uk`/`.europa.eu` domains). This was built for a different reason
  entirely (marking a source as a primary authority for general classification) and
  turned out to directly answer this question too.
- **`single_source`** — `claims.supporting_source_count == 1` is already computed at
  extraction time (`services/api/app/routers/worker.py`'s `store_claims`).
- **`corroborated`** — `≥ 2` distinct `source_id`, refined by one additional check:
  `raw_articles.content_hash` must also differ across the evidence. ADR-0013 is
  explicit that source identity alone cannot license an independence claim ("forty
  outlets carrying one wire story are forty sources and one witness"); two evidence
  articles with byte-identical bodies are exactly that case, reproduced at the claim
  level instead of the story level ADR-0013 was originally about.

Two are not:

- **`disputed`** and **`refuted`** both require deciding whether two DIFFERENTLY-
  WORDED claims about the same fact actually conflict. That is a semantic judgement a
  source count cannot make. PIPELINE.md §11 itself specifies Opus-tier verification
  with an independent second pass for exactly this, which is real, separate,
  larger work — not something to approximate with a keyword-overlap heuristic dressed
  up as a computed signal. CLAUDE.md's "never fabricate" rule is the reason this
  matters here specifically: a `disputed` or `refuted` status this stage cannot
  actually justify is worse than the honest status it would otherwise report.

One clause of the `corroborated` rule is also not implemented: "reliability ≥
threshold". Every source in the corpus currently sits at `reliability_score`'s schema
default (0.400) — nothing in this codebase has ever actively computed it, because
PIPELINE.md §9's per-source reliability needs a correction-rate history from
PUBLISHED articles, which do not exist yet (Phase 4 has not shipped). Gating
`corroborated` on a number nothing has ever computed would make it unreachable for
the entire corpus — a worse outcome than not checking it, and a direct instance of
the same trap ADR-0018 already documented for `us_relevance_score`.

## Decision

`thedrop_database.verification.compute_status` implements the three deterministic
outcomes. `verify_claim` writes the result and `verified_at`; `unverified_claim_ids`
dispatches every claim still at the enum's own `unverified` default, which needs no
separate timestamp column the way extraction's `*_extracted_at` markers do — being
off `unverified` already means "processed" by the enum's own definition.

**Runs on the VPS, inline**, not the desktop PIPELINE.md §11 tags this stage for —
the same reasoning ADR-0015 (clustering) and ADR-0018 (US relevance) already
established: this slice is pure SQL and needs no model. The desktop only enters once
`disputed`/`refuted` are built, since that piece genuinely needs one.

**Not wired to any gate.** PIPELINE.md §11's "a load-bearing claim in a `high` risk
story must be `corroborated` or `authoritative`, otherwise the story is deferred" is a
Phase 4 (article generation) concern — nothing writes stories yet, so there is nothing
downstream to gate. Same reasoning ADR-0018 already used for `US_RELEVANCE_MIN`.

## Consequences

- A claim's `verification_status` today measures "how many genuinely distinct
  sources support this, and does one of them carry government/primary-source
  standing" — a real and useful signal, narrower than "has this been checked for
  internal consistency across the whole story," which `disputed`/`refuted` would add.
- `corroborated` requires two conditions most 2-source claims will actually satisfy
  (distinct source AND distinct content), so this is not merely "at least 2 sources
  found" — a claim reported identically by a wire-fed pair of outlets correctly stays
  `single_source` rather than reading as independently confirmed.
- Once source-credibility scoring (PIPELINE.md §9) exists, the reliability threshold
  clause should be added to `compute_status` — the function already isolates the
  decision rule from the database query specifically so that change is a rule edit,
  not a rewrite.
- `disputed`/`refuted` remain real, separate future work: a model call over a story's
  full claim set, on the desktop, at the Opus tier for high-risk stories, with the
  independent second pass PIPELINE.md specifies. Building it is what actually gates
  publication on load-bearing claims being checked — this ADR's slice alone does not
  yet make that gate meaningful, only measurable.
