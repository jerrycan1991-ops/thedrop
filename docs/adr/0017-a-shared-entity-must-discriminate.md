# ADR-0017: A shared entity licenses a join only if it discriminates

Status: Accepted (Phase 3)

Date: 2026-09-03

## Context

PIPELINE.md §6 requires a shared salient entity before two articles may cluster
together, because embeddings alone happily merge "shooting in Ohio" with "shooting in
Nevada". The rule was written before there was a corpus to test it against.

The first real corpus — 152 articles, fully extracted — says the rule as literally
written does not do its job:

```
  28  PLACE    United States     18% of the corpus
  19  PERSON   Trump             13%
  10  PLACE    Iran
  10  ORG      NASA
   9  OTHER    American
   6  OTHER    Rep
```

Any two of those 28 articles share an entity. The guard passes, and cosine similarity
decides alone — exactly the situation the guard exists to prevent. A US tariff story and
a US shooting both mention the United States. On a US news site that is nearly a
tautology.

Two further observations from the same data: `American` and `Rep` are not entities at
all — they are a nationality adjective and a truncated honorific, both from the model's
MISC label. And `United States` appeared under two spellings, because the tokenizer
splits "U.S." and the aggregator rejoins it as "U. S".

## Decision

A shared entity licenses a join only when it is **discriminative**:

1. **Type.** `OTHER` may not license a join. It is where MISC lands, and MISC is where
   the noise lives. Such entities are still stored — they are real observations — they
   simply cannot justify a merge.
2. **Document frequency.** An entity appearing in more than `ENTITY_GUARD_MAX_DOC_FRACTION`
   of the extracted corpus (default 10%) may not license a join, subject to a floor of
   `ENTITY_GUARD_MIN_DOC_FLOOR` articles (default 5).

The similarity threshold still applies independently. Both conditions are required and
neither substitutes for the other.

Separately, tokenizer artifacts are normalised at extraction: "U. S" → "United States".

## Rationale

- This makes the guard **stricter** than PIPELINE.md specifies, not looser. CLAUDE.md
  forbids weakening a safeguard; tightening one against evidence is the opposite.
- Its only failure mode is under-clustering, which ADR-0015 already establishes as the
  safe direction: duplicate stories are visible and mergeable, a wrongly merged story
  asserting facts about the wrong event is neither.
- The document-frequency rule is ordinary IDF reasoning. A term in 18% of documents
  separates almost nothing; a term in 1% separates a great deal.
- The floor exists because the fraction alone is wrong on a young corpus. At 20
  articles a bare 10% ceiling rejects any entity seen twice, and nothing would ever
  cluster. The floor makes the rule inert until there is enough data for it to mean
  something.
- The thresholds are configuration, not constants in code, because the right values
  depend on corpus composition — a single-topic feed and a general news feed do not
  share an answer.

## Consequences

- `Trump` will not license a join on a US politics site. That is intended: two articles
  mentioning Trump are not thereby about the same event. They may still cluster on a
  rarer shared entity, or on a later consolidation pass.
- An article whose entities are all common or all `OTHER` has no discriminative entity
  and joins nothing. It becomes its own story, which is the correct answer: nothing
  about it says which event it belongs to.
- The thresholds need revisiting as the corpus grows. 10% of 152 articles is 16; 10% of
  50,000 is 5,000, which would exclude almost nothing. A fraction is the right shape for
  now, but a corpus two orders of magnitude larger may want a different rule.
- Entities extracted before this change carry the old spellings. They need
  re-extraction to benefit, which is a backfill, not an automatic correction.

## Amendment, same day: exposure is counted per group, not per row

At 393 articles the ceiling was 40. `Trump` appeared in 97 and was correctly excluded;
`Donald Trump` appeared in 18 and was admitted. One person, two rows, and the ceiling
leaked under the longer form — costing **precision**, which is the one thing the guard
protects.

Exposure is therefore summed across entities of the same type where one name is a
whole-word suffix of the other. The rule is narrow on purpose: it fires only when the
corpus contains both forms, so two people sharing a surname are not grouped, and
whole-word matching keeps `Ian` out of `Iran`. It changes only how exposure is COUNTED —
the rows are not merged and nothing asserts they are the same entity — so its only
possible effect is to exclude more, which is the safe direction.

`America` against `United States` is not a suffix relation and cannot be caught this
way, so it is a named equivalence in the extractor's alias map alongside the tokenizer
repairs. That one is an editorial judgement and is labelled as such: on a US news site a
bare "America" means the United States.
