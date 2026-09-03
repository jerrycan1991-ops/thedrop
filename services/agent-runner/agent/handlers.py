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

from agent import embedding

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


@register("embed_articles")
def embed_articles(payload: dict[str, Any]) -> dict[str, Any]:
    """Embed a batch of articles on the GPU (ADR-0005).

    Returns the vectors in `embeddings`; the RUNNER posts them and strips them before
    completing, so they never reach `jobs.result`. Keeping that here would mean giving
    every handler a client and knowledge of the protocol, which is the runner's job.

    Order is preserved between input items and output vectors, and the article id is
    carried through explicitly rather than by position -- a silent reordering would
    attach every article to the wrong vector, and nothing downstream could detect it.
    """
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise NonRetryableError("embed_articles payload has no items")

    texts: list[str] = []
    ids: list[str] = []
    for item in items:
        article_id = (item or {}).get("id")
        text = (item or {}).get("text")
        if not article_id or not text:
            raise NonRetryableError(f"embed_articles item is missing id or text: {item!r}")
        ids.append(str(article_id))
        texts.append(str(text))

    vectors = embedding.encode(texts)
    if len(vectors) != len(ids):
        # Cannot happen with a sane encoder, and would silently mis-assign every vector
        # if it did. Non-retryable: the same input would produce the same mismatch.
        raise NonRetryableError(f"encoder returned {len(vectors)} vectors for {len(ids)} texts")

    logger.info("embedded batch", extra={"count": len(ids)})
    return {
        "model": embedding.model_name(),
        "embeddings": [{"id": i, "vector": v} for i, v in zip(ids, vectors, strict=True)],
    }


if not embedding.is_available():
    # Unregister rather than fail at claim time. The runner advertises only what it can
    # dispatch, so a desktop without torch never receives embedding work and the
    # batches simply wait -- which is what ADR-0005 already promises when the desktop is
    # offline. Warned, because a broken install would otherwise look like idleness.
    del _REGISTRY["embed_articles"]
    logger.warning(
        "sentence-transformers is not installed; not advertising 'embed_articles'. "
        "Install the desktop group: uv sync --group desktop"
    )
