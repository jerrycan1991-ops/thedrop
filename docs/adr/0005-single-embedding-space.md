# ADR-0005: One 384-dimension embedding space, computed only on the desktop

Status: Accepted (Phase 0)
Date: 2026-08-30

## Context

Deduplication and clustering need embeddings. Ingestion happens on the VPS; the GPU is on the desktop. Running any embedding model on the VPS means installing torch or ONNX Runtime — 2–4 GB of dependencies and real CPU contention on a 4-core box.

## Decision

- The VPS computes no embeddings. It performs cheap deduplication only: canonical URL hash, content hash, and 64-bit SimHash over title plus lede.
- The desktop computes all embeddings with `bge-small-en-v1.5` (384 dimensions, normalized) and posts them back.
- One vector space for everything. `raw_articles.embedding` and `stories.centroid` are both `vector(384)`.

## Rationale

- SimHash catches near-identical syndication in single-digit milliseconds with no model. That is the overwhelming majority of duplicates in a news feed.
- Genuine semantic near-duplicates are better handled at clustering than at ingest, so deferring them to the desktop loses nothing.
- 384 dimensions keeps the HNSW index small and fast, which matters when Postgres has a 1 GB buffer pool.
- A single model means every stored vector is comparable. Mixed dimensions or mixed models silently corrupt similarity search, and the failure is very hard to notice.

## Consequences

- Stories are not clustered while the desktop is offline. Acceptable: ingestion continues, the backlog drains on return, and nothing is published unclustered anyway.
- Changing the embedding model requires a full backfill and a new ADR. The model revision is carried in config so a mismatch is detectable.
- If VPS-side semantic dedup ever becomes necessary, `fastembed` with the same model is the drop-in — same 384 dimensions, same space.
