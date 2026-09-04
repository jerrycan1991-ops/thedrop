# ADR-0023: contradiction detection runs on the same local Ollama model, not Opus

Status: Accepted (Phase 3), quality still being measured

Date: 2026-09-04

## Context

PIPELINE.md §11 specifies Opus-tier verification, with an independent second pass for
high-risk stories, for the stage that decides whether two claims about a story
genuinely conflict. This stage produces `disputed` and `refuted` -- the two
verification outcomes `thedrop_database.verification` (ADR-0022) deliberately leaves
unimplemented, because deciding whether two DIFFERENTLY-WORDED claims about the same
fact actually disagree is a semantic judgement a source count cannot make.

Following ADR-0020's precedent, the operator was asked to choose between Ollama (free,
same model as extraction, unbenchmarked for this harder task) and an Anthropic tier
for this stage, and was explicitly told the tradeoff before choosing: comparing
meaning across two differently-worded claims is plausibly a harder reasoning task than
extraction itself, for the highest-stakes verification stage in the pipeline. The
operator chose Ollama, matching the extraction model
(`services/agent-runner/agent/contradictions.py` piggybacks on
`CLAIM_EXTRACT_OLLAMA_MODEL` by default -- see `model_name()`).

## What was measured

Before any dispatch wiring was built, `find_contradictions()` was run against the real
local Ollama server (not mocked) on three constructed cases:

1. **A genuine contradiction** -- one claim asserting a jury reached a verdict,
   another asserting the jury remained deadlocked, about the same case. Correctly
   flagged.
2. **A prompt injection attempt** inside a claim's own text, addressed at the model
   directly. Correctly ignored as content, `injection_detected` set `true`, and the
   rest of the batch was still reviewed normally -- the SECURITY.md §6 behavior this
   stage needs.
3. **A refinement, not a contradiction** -- an early "several injured" report and a
   later, more complete "twelve injured" count for the same event. The system prompt
   contains an explicit instruction not to flag this exact pattern, with this near-
   identical example written into it verbatim (see `_SYSTEM_PROMPT` in
   `contradictions.py`). The model flagged it as a contradiction anyway.

Case 3 is a real, observed failure, not a hypothetical one, and it is the kind of
mistake that matters most here: it would mark an accurate, updated casualty count as
`disputed` next to the report it superseded, which is a worse outcome than leaving
both at `single_source` -- a reader sees "sources conflict" on two claims that do not.

## Decision

`services/agent-runner/agent/contradictions.py` ships on Ollama, `qwen2.5:7b` by
default, wired end to end: `find_contradictions` (desktop) →
`POST /api/v1/worker/contradictions` (`services/api/app/routers/worker.py`, function
`store_contradictions`) → `thedrop_database.contradiction_queue` dispatch. This is a
deliberate, informed choice, not an oversight: the operator was shown the case-3
failure before deciding to proceed, consistent with CLAUDE.md's "never fabricate" --
this ADR is the honest record of what was actually observed, not a claim that the
model performs well.

The authoritative/refuted-vs-both-disputed decision logic
(`store_contradictions`) narrows the blast radius of a false positive like case 3
somewhat: a false contradiction between two `single_source` claims produces
`disputed` on both, not `refuted` on either, and `disputed` is a softer signal than
outright rejection. It does not fix the false positive itself.

## Consequences

- **This is explicitly provisional**, same status as ADR-0020's extraction model, and
  for a stage with a demonstrated failure mode, not just an unmeasured one. Before a
  reader-facing template ever renders `disputed`/`refuted` from this stage's output,
  it needs a labeled benchmark -- a real sample of genuine contradictions vs.
  refinements vs. unrelated claims, with a measured false-positive rate, not three
  constructed examples.
- Until that benchmark exists, `disputed`/`refuted` claims produced by this stage
  should be treated as a signal for editorial review, not as ground truth a template
  renders unquestioned -- no template currently does; this note constrains what one
  may safely do later, not a change made here.
- The false positive observed (case 3) is specifically a "refinement mistaken for
  contradiction" pattern. If the eventual benchmark confirms this is systematic rather
  than a one-off, the fix belongs in the prompt or the model choice, not in the
  storage layer -- `store_contradictions` has no way to distinguish a correct flag
  from an incorrect one after the fact.
- Same properties as ADR-0020 otherwise: switchable via
  `CONTRADICTION_CHECK_OLLAMA_MODEL`/an eventual `contradiction_check_provider`
  setting without a schema change, `ai_runs.provider`/`.model` already record which
  model produced which result, and `ai_runs.cost` stays 0/unset for the ollama path.

## Related

ADR-0020 (claim extraction's own Ollama decision and benchmark), ADR-0022
(cross-source verification's deterministic subset, and why disputed/refuted were left
for this stage instead).
