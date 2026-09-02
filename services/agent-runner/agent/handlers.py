"""Job handler registry.

A handler takes a job payload and returns a JSON-serialisable result dict, which the
runner posts back to `/jobs/{id}/complete`. Raising is how a handler reports failure;
the runner decides retryability.

The registry is also what the runner advertises when claiming, so the API can only ever
lease work this process can actually dispatch. Adding a handler is therefore the whole
of "teaching the desktop a new job type" -- there is no second list to keep in sync.

Phase 2 adds ingestion handlers, Phase 3 embeddings and scoring on the 4070, Phase 4
generation. The skeleton ships with `noop` only, which is enough to prove the
claim/complete round trip end to end.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], dict[str, Any]]

_REGISTRY: dict[str, Handler] = {}


class NonRetryableError(Exception):
    """Raise when retrying cannot possibly help -- malformed payload, missing handler.

    Anything else is treated as retryable, because the common failures here are
    transient: a model server not up yet, a provider rate limit, a disk hiccup.
    """


def register(job_type: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if job_type in _REGISTRY:
            raise RuntimeError(f"handler for {job_type!r} is already registered")
        _REGISTRY[job_type] = fn
        return fn

    return decorator


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def dispatch(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = _REGISTRY.get(job_type)
    if handler is None:
        # The API only leases types we advertised, so this means the registry and the
        # advertised list disagree -- a bug here, not a transient fault.
        raise NonRetryableError(f"no handler registered for job type {job_type!r}")
    return handler(payload)


@register("noop")
def noop(payload: dict[str, Any]) -> dict[str, Any]:
    """Does nothing, on purpose.

    Exists so the desktop-VPS contract can be exercised without any model, GPU or
    provider being involved: enqueue a `noop` job, watch it get claimed, completed, and
    disappear from the queue. When that works, everything after it is just handlers.

    `sleep_seconds` lets a test hold a lease open long enough to observe the heartbeat
    extending it.
    """
    sleep_seconds = float(payload.get("sleep_seconds", 0) or 0)
    if sleep_seconds > 0:
        time.sleep(min(sleep_seconds, 60))
    logger.info("noop handler ran", extra={"slept": sleep_seconds})
    return {"ok": True, "echo": payload, "sleptSeconds": sleep_seconds}
