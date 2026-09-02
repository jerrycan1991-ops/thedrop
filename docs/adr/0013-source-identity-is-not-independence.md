# ADR-0013: A source is a hostname; independence is a separate judgement

Status: Accepted (Phase 2)

Date: 2026-09-02

## Context

`resolve_source` creates one `sources` row per hostname, stripping only a leading
`www.`. Adding NASA's news feed produced two rows on the first poll:

```
nasa.gov          authority=True
science.nasa.gov  authority=True
```

Both are NASA. As *sources* that is arguably right — they are different publications
with potentially different editorial handling, and reliability is tracked per source.

As *independent corroboration* it is wrong, and the difference matters because
`CLAUDE.md` makes a hard rule of it:

> High-risk categories (politics, elections, crime, deaths, legal accusations, health,
> financial-market claims, war, allegations, public safety, celebrity death/arrest)
> require two independent credible sources **or** a directly relevant authoritative
> primary source.

A verification step that counts distinct `source_id` values would treat `nasa.gov` and
`science.nasa.gov` as two independent confirmations of the same claim. They are one
organisation restating itself. The rule would report as satisfied while the property it
protects — that two parties arrived at the same fact separately — was never true.

This is not hypothetical to subdomains either. Wire syndication is the same failure at
larger scale: forty outlets carrying one AP story are forty sources and one witness.

Nothing published yet, so nothing is broken today. It is being recorded now because the
failure is silent, and the place it bites (Phase 3 cross-source verification) is far
from the place it originates (Phase 2 ingestion).

## Options considered

1. **Collapse to the registrable domain.** `science.nasa.gov` becomes `nasa.gov`.
   Correct for NASA. Wrong for `blogs.example.com` versus `example.com`, where a
   publisher's blog and its newsroom genuinely differ in editorial standard, and wrong
   for any platform hosting independent publishers on subdomains. It also destroys
   information: once collapsed, the distinction cannot be recovered.
2. **An `organisation` grouping on `sources`.** Accurate, and it models the real thing.
   Needs a schema change and, more importantly, human curation — no algorithm knows
   that Reuters and its licensees are one witness.
3. **Keep sources per-hostname, and make independence an explicit check.** Source
   identity answers "who published this"; independence answers "did these two arrive at
   it separately". Different questions.

## Decision

Option 3. A `sources` row remains one hostname, and **nothing may infer independence
from source identity**.

Concretely, when cross-source verification is built:

- It must not satisfy a corroboration requirement by counting distinct `source_id`.
- It must consider at least: shared registrable domain, shared organisation, and
  syndication (the same body text arriving from multiple sources — which the
  `content_hash` check already detects and records, and which is exactly the signal
  needed here).
- Where independence cannot be established, the safe answer is "not corroborated". A
  story waits. `CLAUDE.md`: quota never publishes anything.

## Rationale

- The two questions have different right answers and different data. Conflating them
  means fixing one breaks the other.
- Per-hostname identity is the version that loses no information. Grouping can be added
  later from richer data; a collapsed domain cannot be un-collapsed.
- Syndication is the dominant real-world case and is *already* visible: `store_item`
  records an `exact_duplicate` when the same body arrives under a different URL. Three
  such rows appeared from `doj-press` within minutes of adding it. That signal is
  already in the database, waiting for a consumer.
- Deciding this at ingestion time would be deciding it with the least information. The
  verification step knows the claim, the category and the risk tier.

## Consequences

- `sources` will contain rows that are the same organisation. That is expected, not a
  data-quality problem to be cleaned up.
- Any future code that counts sources to establish corroboration is a defect, whatever
  it looks like locally. This ADR is what it should be checked against.
- Phase 3 carries the cost: cross-source verification is more work than counting rows,
  and will need an organisation mapping that a human maintains.
- Until that exists, no automated corroboration decision may be trusted for a high-risk
  story. The one-authoritative-primary-source path (a `.gov` statement about itself)
  remains available and is unaffected, because it never depended on independence.
