# ADR-0016: `dslim/bert-base-NER` for the clustering guard, tuned for precision

Status: Accepted (Phase 3)

Date: 2026-09-03

## Context

PIPELINE.md §6 will not let two articles cluster together unless they share a salient
entity, because embeddings alone happily merge "shooting in Ohio" with "shooting in
Nevada". That requires named-entity recognition, which requires a model, which under
CLAUDE.md's resource discipline means the desktop.

The two errors are not symmetric, and that decides everything else here:

- a **missed** entity means two articles do not join. Over-splitting: visible as
  duplicate stories, fixable by the consolidation pass or a human.
- a **false** entity that happens to match another article's means two different events
  become one story asserting facts about the wrong one. Nothing downstream can detect
  it, and by the time it is visible it has been published.

## Options considered

1. **spaCy `en_core_web_trf`.** Best type coverage — it separates EVENT, LAW and
   PRODUCT, which map onto `EntityType` values a transformer NER model does not
   produce. Costs a second runtime and a second model stack on the desktop for types
   the guard does not use.
2. **spaCy `en_core_web_sm`.** Cheap, but its NER is markedly weaker, and weaker NER
   fails in the expensive direction here.
3. **`dslim/bert-base-NER` through transformers.** Four labels — PER, ORG, LOC, MISC.
   No new runtime: transformers and torch are already installed for embeddings
   (ADR-0005), so this adds a model download and nothing else.

## Decision

Option 3, with predictions below `ENTITY_MIN_CONFIDENCE` (default 0.90) discarded
rather than kept as weak signal.

Labels map to `EntityType` as PER→PERSON, ORG→ORG, LOC→PLACE, MISC→OTHER. The richer
types the schema allows are simply not produced at this stage.

## Rationale

- A guard needs to know that Ohio is not Nevada. PER, ORG and LOC carry that; EVENT and
  LEGISLATION do not add to it.
- Reusing the embedding runtime keeps the desktop to one model stack. A second one
  would be a second thing to install, version and break.
- The high threshold follows directly from the asymmetry above. Discarding a
  low-confidence prediction costs a merge that should have happened; keeping one risks
  a merge that should not have.
- Verified on the target hardware before being adopted, not after: the two shootings
  above share no entity, while a second account of the same shooting does share one.
  Both are pinned in `tests/test_entity_model_gpu.py`.

## Consequences

- MISC collapses nationalities, events and works into OTHER. Acceptable for the guard,
  and honest — it records what the model actually predicted rather than guessing a type
  it never emitted.
- Story-level extraction (PIPELINE.md §12) will want the richer taxonomy and may adopt
  spaCy for that pass. Nothing here blocks it: `entities.entity_type` already accepts
  the full set.
- Surface forms are cleaned conservatively — punctuation and leading articles only.
  Normalising harder ("Donald Trump" → "Trump") is entity RESOLUTION, a different
  problem with a different failure mode, and doing it badly here would merge distinct
  people.
- Changing the model changes which entities exist, and therefore which stories would
  have clustered. Re-extraction replaces an article's entities rather than merging, so
  a model change is a backfill, not an accumulation.
