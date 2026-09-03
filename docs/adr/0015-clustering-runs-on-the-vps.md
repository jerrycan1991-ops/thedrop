# ADR-0015: Incremental clustering runs on the VPS; the desktop does the parts that need a model

Status: Accepted (Phase 3)

Date: 2026-09-03

## Context

`PIPELINE.md` assigns story clustering to the desktop as job `cluster`, alongside
embedding, scoring and generation. Implementing it surfaced a contradiction with
ADR-0001.

Step 1 of the documented algorithm is:

> For each new embedding, query `stories.centroid` for nearest clusters active in the
> last 48 h (`ORDER BY centroid <=> $1 LIMIT 10`).

That is a database query against a pgvector index. **The desktop holds no database
credentials and never will** — its only interface to the VPS is authenticated HTTPS to
`/api/v1/worker/*`, and SECURITY.md classifies it as semi-trusted because it executes
model output. So the desktop cannot perform step 1 at all.

The rule that put clustering on the desktop is CLAUDE.md's resource discipline: *no ML
runtimes on the VPS*. That rule is about **runtimes**, not about the word "clustering".
Taking the algorithm apart by what it actually needs:

| Step | Needs | Where it can run |
|---|---|---|
| Nearest active centroids | pgvector query | VPS only (desktop has no DB) |
| Cosine similarity vs threshold | one dot product | either |
| Shared-entity guard | entities already stored | either |
| Centroid update (running mean) | arithmetic | either |
| Extracting entities from an article | an NER model | desktop only |
| Periodic HDBSCAN consolidation | scikit-learn/hdbscan | desktop only |

Nothing in incremental clustering needs a model. Everything in it needs the database.

## Options considered

1. **Give the desktop database credentials.** Rejected outright. ADR-0001 and
   SECURITY.md put the credential boundary where they do precisely because that machine
   runs model output. Clustering is not a good enough reason to move it.
2. **Ship the candidate set to the desktop and take the decision back.** The VPS runs
   the pgvector query, posts ten centroids to the desktop, the desktop compares and
   answers. Technically possible, but it is a network round trip and a lease to compute
   one dot product, and the story stays unclustered whenever the desktop is offline —
   for no gain, since no model is involved.
3. **Split by capability rather than by name.** Incremental clustering on the VPS;
   entity extraction and periodic consolidation on the desktop.

## Decision

Option 3.

- **VPS, in the ingest pipeline:** nearest-centroid lookup, threshold comparison,
  shared-entity guard, join-or-create, centroid update. Pure SQL and arithmetic.
- **Desktop, as jobs:** entity extraction for new articles (needs NER), and the
  periodic HDBSCAN consolidation pass (needs scikit-learn).

`PIPELINE.md` §6 is amended to match. The pipeline table's "DESKTOP" for step 6 was
written before the credential boundary was settled in ADR-0001.

## Rationale

- The constraint is not negotiable in the direction the old design assumed: no
  credentials on the desktop means no `ORDER BY centroid <=> $1` on the desktop.
- The resource rule is satisfied. Incremental clustering adds no runtime to the VPS —
  pgvector is already installed and already used for the `raw_articles` embedding
  column. The cost is one indexed nearest-neighbour query per new article.
- It removes a dependency the public site should not have. ARCHITECTURE.md §3 requires
  the site to work with the desktop offline; under option 2 clustering would stall
  whenever the desktop was down, which is exactly the coupling ADR-0001 exists to
  prevent. Under this decision only entity extraction stalls, and the guard below turns
  that into under-clustering rather than wrong clustering.

## Consequences

- **The shared-entity guard makes entity extraction a hard dependency of joining.**
  PIPELINE.md §6 is explicit that entity overlap is a correctness guard, not an
  optimization: embeddings alone happily merge "shooting in Ohio" with "shooting in
  Nevada". Until extraction lands, no article can satisfy the guard, so every article
  becomes its own story.

  That is the safe failure direction and it is deliberate. Over-splitting produces
  duplicate stories, which the consolidation pass merges and which a human can see.
  Wrongly merging two shootings produces one story asserting facts about the wrong
  event, which nothing downstream can detect. Nothing is published from clusters yet,
  so the cost today is zero.

  It must not be "fixed" by lowering the guard to similarity alone.
- A desktop that is offline for a day leaves that day's articles unclustered but
  correctly stored, embedded and visible. They cluster when extraction catches up.
- HDBSCAN consolidation will need the vectors shipped to the desktop in a job payload,
  the same way embedding ships text. That is a larger payload and will need its own
  bound.
