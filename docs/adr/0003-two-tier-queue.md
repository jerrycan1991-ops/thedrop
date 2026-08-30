# ADR-0003: Two-tier queue — Celery on the VPS, HTTP job leases for the desktop

Status: Accepted (Phase 0)
Date: 2026-08-30

## Context

Two very different kinds of background work: fast, frequent, local VPS tasks (poll a feed, roll up analytics, revalidate a path) and slow, heavy, remote desktop tasks (embed, cluster, write, render).

## Decision

- VPS-local work: Celery with a Redis broker, one worker process, three named queues (`ingest`, `maintain`, `publish`), embedded beat.
- Desktop work: a `jobs` table in Postgres with an HTTP claim/lease protocol. Not Celery.

## Rationale

- Celery is excellent for local, low-latency, high-frequency tasks with a reliable broker connection. It is a poor fit for a remote worker on a home connection that may vanish mid-task.
- The lease table makes desktop work first-class data: visible in the admin UI, queryable, auditable, restartable, and joinable to stories and articles. Celery task state is opaque by comparison.
- Redis stays on loopback. Exposing it, even over a VPN, widens the blast radius for no benefit.
- `SELECT ... FOR UPDATE SKIP LOCKED` gives correct concurrent claiming without a distributed lock.

## Consequences

- Two queue mechanisms to reason about. The boundary is unambiguous: if it runs on the desktop, it is a `jobs` row; otherwise it is a Celery task.
- Beat is embedded in the single worker (`-B`), so that unit must never be scaled past one replica. Documented in the unit file and in DEPLOYMENT.md.
