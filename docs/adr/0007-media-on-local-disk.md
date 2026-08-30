# ADR-0007: Media on local disk behind a storage abstraction

Status: Accepted (Phase 0)
Date: 2026-08-30

## Context

The pipeline generates roughly 100 images and 15 videos a day — about 5.5 GB per month. Options were self-hosted object storage (MinIO), a cloud bucket (S3 / R2 / Spaces), or local disk.

## Decision

Local disk at `/var/www/thedrop/media/`, served by Next.js from a symlinked public directory, behind a `MediaStorage` interface with `LocalDiskStorage` as the Phase 1 implementation.

## Rationale

- MinIO would cost roughly 400 MB of RAM and add a failure domain, to solve a problem we do not have at 5 GB per month.
- A cloud bucket adds latency, cost and a credential to manage before there is any traffic to justify it.
- Serving from Next.js needs no nginx change, which is a hard constraint in Phase 1.
- Content-addressed paths (asset UUID in the path) make every asset immutable, so caching is trivially correct.

## Consequences

- Media lives on the same disk as the database. Disk alerting at 75 % and a retention sweep are mandatory, not optional.
- Media is not on a CDN, so image bandwidth is served by the VPS. Acceptable at launch traffic; putting a CDN in front of the domain fixes it later with no code change.
- The interface means switching to S3-compatible storage is a config swap plus a one-time sync. Revisit when monthly media exceeds roughly 50 GB, or when image bandwidth becomes a visible cost.
