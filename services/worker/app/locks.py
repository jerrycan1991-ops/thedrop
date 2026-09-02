"""Per-provider in-flight locks, in Redis.

Beat ticks every 60 seconds. A poll that takes longer than its interval would be
dispatched again on the next tick, and two workers would fetch the same feed
concurrently. The `url_hash` constraint means that cannot corrupt anything -- one of
them simply loses the race and records duplicates -- but it doubles the request rate at
a publisher who never agreed to it, and PIPELINE.md's rate limits become fiction.

`SET key value NX EX ttl` is the whole mechanism: atomic, self-expiring, and it cannot
strand a provider if a worker dies mid-poll, which a lock without a TTL would.

Deliberately not Redlock or a lock library. There is one Redis and one worker
(ADR-0003), so the failure modes those solve do not exist here, and a dependency that
implies otherwise would be misleading.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import redis

logger = logging.getLogger(__name__)

#: Long enough to outlast a slow feed, short enough that a killed worker does not block
#: a provider for an hour. A poll that genuinely exceeds this is a bug worth noticing.
DEFAULT_LOCK_TTL_SECONDS = 600

_KEY_PREFIX = "thedrop:lock:provider:"


def _client() -> redis.Redis:
    url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
    return redis.Redis.from_url(url, decode_responses=True)


def _owner_token() -> str:
    """Identifies the holder, so a lock can only be released by whoever took it."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@contextmanager
def provider_lock(
    slug: str, ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
) -> Iterator[bool]:
    """Hold the lock for `slug` if it is free. Yields whether it was acquired.

    Yielding False rather than raising: a provider already being polled is the normal
    outcome of a tick arriving while the previous poll runs, not an error anyone should
    see in a log at WARNING.
    """
    key = f"{_KEY_PREFIX}{slug}"
    token = _owner_token()

    try:
        client = _client()
        acquired = bool(client.set(key, token, nx=True, ex=ttl_seconds))
    except (redis.RedisError, KeyError) as exc:
        # Redis being down must not stop ingestion. The consequence of proceeding
        # without a lock is a possible duplicate fetch, which the url_hash constraint
        # already makes harmless; the consequence of refusing would be no news at all.
        logger.warning("provider lock unavailable, proceeding without it: %s", exc)
        yield True
        return

    if not acquired:
        logger.debug("provider %s is already being polled", slug)
        yield False
        return

    try:
        yield True
    finally:
        # Compare-and-delete. A plain DELETE would let a slow poll whose lock had
        # already expired delete the lock a *different* worker now legitimately holds.
        try:
            script = client.register_script(
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end"
            )
            script(keys=[key], args=[token])
        except redis.RedisError as exc:
            # The TTL will clear it; nothing is stranded.
            logger.warning("could not release provider lock %s: %s", slug, exc)
