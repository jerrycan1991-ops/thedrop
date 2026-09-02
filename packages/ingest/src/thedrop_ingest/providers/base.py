"""The provider contract (PIPELINE.md §2).

Every adapter implements `NewsProvider`. Nothing downstream imports a provider module
-- the pipeline depends only on `NormalizedItem` -- so adding a provider can never
change the pipeline, and a broken adapter cannot corrupt anything beyond its own page.

Guards that belong to *every* provider live here rather than being reimplemented per
adapter, because a guard that each adapter has to remember is a guard that will
eventually be forgotten:

  * response size cap (2 MB)
  * items-per-run cap
  * circuit breaker state, evaluated by the caller against the `providers` row
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from thedrop_ingest.normalize import NormalizedItem

#: PIPELINE.md §2. A feed larger than this is a bug or an attack, not a news source.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Per-run item cap. Bounds the work one poll can create regardless of what a provider
#: returns, so a misbehaving feed cannot flood the queue.
MAX_ITEMS_PER_RUN = 200


class ProviderError(RuntimeError):
    """Adapter could not produce a page. Counts toward the circuit breaker."""


class ResponseTooLargeError(ProviderError):
    """Response exceeded MAX_RESPONSE_BYTES and was abandoned unread."""


@dataclass(frozen=True)
class ProviderPage:
    """One poll's worth of results.

    `next_cursor` is opaque to the pipeline and stored verbatim in `providers.cursor`;
    only the adapter that produced it ever interprets it.
    """

    items: tuple[NormalizedItem, ...]
    next_cursor: str | None = None
    rate_limit_remaining: int | None = None
    #: Items the adapter saw and deliberately skipped, with a reason. Surfaced in the
    #: admin so "why did this feed produce nothing" has an answer that is not a guess.
    skipped: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProviderHealth:
    ok: bool
    detail: str = ""
    checked_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class NewsProvider(Protocol):
    slug: str

    def fetch(self, since: datetime, cursor: str | None) -> ProviderPage: ...

    def health(self) -> ProviderHealth: ...


def read_capped(chunks: Iterator[bytes], limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read a streamed body, abandoning it the moment it exceeds `limit`.

    Streaming rather than `response.content` is the point: reading a 4 GB body to
    discover it is too large defeats the cap. This stops at the first chunk that
    crosses the line, so the memory ceiling holds even against a hostile server.
    """
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise ResponseTooLargeError(f"response exceeded {limit} bytes")
    return bytes(buffer)
