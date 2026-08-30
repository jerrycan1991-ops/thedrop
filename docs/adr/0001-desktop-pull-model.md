# ADR-0001: The desktop pulls work over HTTPS; the VPS never dials the desktop

Status: Accepted (Phase 0)
Date: 2026-08-30

## Context

Heavy AI and media work must run on the private RTX 4070 SUPER desktop. The desktop sits behind residential NAT with a dynamic IP and no inbound ports, and must not be exposed to the public internet. It may also be offline for hours at a time.

## Options considered

1. VPS pushes jobs to the desktop over HTTP. Requires an inbound port, port forwarding, dynamic DNS, and TLS on a home connection.
2. Persistent tunnel (ngrok / Cloudflare Tunnel / reverse SSH) so the VPS can reach the desktop.
3. Desktop is a Celery worker connecting directly to Redis on the VPS over a VPN.
4. Desktop long-polls an authenticated HTTPS lease API on the VPS.

## Decision

Option 4. The desktop makes only outbound HTTPS requests to `https://thedrop.channel/api/v1/worker/*`, claiming jobs from a durable lease table in Postgres.

## Rationale

- Zero inbound exposure on the desktop. Nothing to firewall, nothing to forward.
- Survives IP changes, router reboots and ISP outages with no configuration.
- Offline tolerance is free: jobs simply stay `QUEUED`. Options 1 and 2 fail loudly when the desktop disappears; option 3 leaves Celery in a half-broken reconnect state and requires exposing Redis, which SECURITY.md forbids.
- The lease table gives durable, auditable job state with retry, backoff and reaping. Celery over a flaky WAN gives none of that.
- An optional WireGuard tunnel can still be layered on later for defense in depth without changing the protocol.

## Consequences

- We implement claim/heartbeat/complete/fail endpoints and a lease reaper ourselves. Roughly 200 lines, and the price of the properties above.
- Polling adds latency, bounded by the long-poll timeout (~25 s worst case). Irrelevant for a pipeline measured in minutes.
- The VPS cannot initiate urgent work on the desktop. Priority is expressed in the queue, not by pushing.
