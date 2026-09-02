"""RSS 2.0 and Atom adapter.

The first provider on purpose: no API key, no quota, no vendor. A publisher's feed is
the most direct statement of what they published and when, and it can be tested against
recorded fixtures with no network (ROADMAP Phase 2 exit criteria).

## Why stdlib XML, and how the attack surface is closed

A feed is untrusted input from a third party (CLAUDE.md). `xml.etree` has two known
hazards, and `defusedxml` exists to solve them. Both are closed here without it:

  * **External entities / XXE** -- `xml.etree.ElementTree` does not resolve external
    entities. An undefined entity reference raises `ParseError` rather than fetching
    anything, so file and network disclosure are not reachable.
  * **Entity expansion ("billion laughs")** -- needs a DTD to declare the entities, so
    this adapter rejects any document containing a DOCTYPE outright. No legitimate RSS
    or Atom feed needs one. Rejecting is safe where sanitising would be guesswork.

Combined with the 2 MB streamed cap in `base.read_capped`, a hostile feed cannot exhaust
memory or reach anything on the host.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from thedrop_ingest.normalize import (
    NormalizedItem,
    canonicalize_url,
    html_to_text,
    sanitize_text,
)
from thedrop_ingest.providers.base import (
    MAX_ITEMS_PER_RUN,
    ProviderError,
    ProviderHealth,
    ProviderPage,
    read_capped,
)

logger = logging.getLogger(__name__)

#: A DOCTYPE is the prerequisite for entity-expansion attacks and has no legitimate use
#: in a syndication feed. Checked on raw bytes, before any parsing happens.
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)

_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _parse_date(value: str) -> datetime | None:
    """RSS dates are RFC 822, Atom dates are RFC 3339. Try both, guess neither."""
    value = value.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    # A naive timestamp is assumed UTC rather than dropped: the alternative is
    # discarding a real publication time, and the estimate flag records the doubt.
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RSSProvider:
    """Polls one feed URL.

    One instance per `providers` row. `config` carries `{"feed_url": ...}`; the source
    a feed belongs to is resolved by the caller from the item's domain, because one
    feed can legitimately carry several publishers.
    """

    def __init__(
        self,
        slug: str,
        feed_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "thedrop-ingest/0.1 (+https://thedrop.channel)",
    ) -> None:
        self.slug = slug
        self.feed_url = feed_url
        self._timeout = timeout
        self._user_agent = user_agent
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ fetch
    def fetch(self, since: datetime, cursor: str | None = None) -> ProviderPage:
        """Fetch and normalize the feed.

        `cursor` holds the newest `published_at` seen last run, so a feed that has not
        changed produces no items without us having to diff it. It is advisory only --
        the url_hash constraint is what actually guarantees no duplicate is stored.
        """
        headers = {}
        # If-Modified-Since is politeness and bandwidth, not correctness.
        if cursor:
            headers["If-Modified-Since"] = cursor

        try:
            with self._http().stream("GET", self.feed_url, headers=headers) as response:
                if response.status_code == 304:
                    return ProviderPage(items=(), next_cursor=cursor)
                if response.status_code >= 400:
                    raise ProviderError(f"{self.feed_url} returned {response.status_code}")
                body = read_capped(response.iter_bytes())
                last_modified = response.headers.get("last-modified")
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.feed_url}: {exc}") from exc

        items, skipped = self._parse(body, since)
        return ProviderPage(
            items=items,
            next_cursor=last_modified or cursor,
            skipped=skipped,
        )

    # ------------------------------------------------------------------ parse
    def _parse(
        self, body: bytes, since: datetime
    ) -> tuple[tuple[NormalizedItem, ...], tuple[tuple[str, str], ...]]:
        if _DOCTYPE.search(body):
            # See the module docstring: a DOCTYPE is the prerequisite for entity
            # expansion and has no legitimate use here.
            raise ProviderError("feed contains a DOCTYPE declaration; refusing to parse")

        try:
            root = ElementTree.fromstring(body)  # noqa: S314 - DOCTYPE rejected above
        except ElementTree.ParseError as exc:
            raise ProviderError(f"malformed feed: {exc}") from exc

        entries = root.findall(".//item") or root.findall(f".//{_ATOM}entry")

        items: list[NormalizedItem] = []
        skipped: list[tuple[str, str]] = []

        for entry in entries[:MAX_ITEMS_PER_RUN]:
            try:
                item = self._entry_to_item(entry)
            except ValueError as exc:
                skipped.append((_text(entry.find("title")) or "<untitled>", str(exc)))
                continue

            published = _parse_date(item.published_at_iso or "")
            if published and published < since:
                skipped.append((item.canonical_url, "older than `since`"))
                continue
            items.append(item)

        return tuple(items), tuple(skipped)

    def _entry_to_item(self, entry: ElementTree.Element) -> NormalizedItem:
        link = self._link(entry)
        if not link:
            raise ValueError("entry has no usable link")

        title_raw = _text(entry.find("title")) or _text(entry.find(f"{_ATOM}title"))
        if not title_raw:
            raise ValueError("entry has no title")

        body_html = (
            _text(entry.find(f"{_CONTENT}encoded"))
            or _text(entry.find("description"))
            or _text(entry.find(f"{_ATOM}content"))
            or _text(entry.find(f"{_ATOM}summary"))
        )
        body_plain, hidden_blocks = html_to_text(body_html)

        # Title and body are scanned separately so a flag names which field carried it.
        title, title_flags = sanitize_text(title_raw)
        body_text, body_flags = sanitize_text(body_plain)

        flags: dict[str, Any] = {}
        if title_flags.get("patterns") or title_flags.get("invisible_chars"):
            flags["title"] = title_flags
        if body_flags.get("patterns") or body_flags.get("invisible_chars"):
            flags["body"] = body_flags
        if hidden_blocks:
            flags["hidden_html_blocks"] = hidden_blocks
        # Always present, so "scanned and clean" is distinguishable from "never scanned".
        flags.setdefault("patterns", [])

        published = _parse_date(
            _text(entry.find("pubDate"))
            or _text(entry.find(f"{_ATOM}published"))
            or _text(entry.find(f"{_ATOM}updated"))
            or _text(entry.find(f"{_DC}date"))
        )
        # A missing timestamp falls back to discovery time and SAYS SO. Inventing one
        # would be a fabricated fact, and downstream freshness logic would believe it.
        estimated = published is None
        if published is None:
            published = datetime.now(UTC)

        authors = tuple(
            a
            for a in (
                _text(entry.find("author")),
                _text(entry.find(f"{_ATOM}author/{_ATOM}name")),
                _text(entry.find(f"{_DC}creator")),
            )
            if a
        )

        return NormalizedItem(
            canonical_url=canonicalize_url(link),
            original_url=link,
            title=title,
            body_text=body_text,
            published_at_iso=published.isoformat(),
            timestamp_estimated=estimated,
            authors=authors,
            image_urls=self._images(entry),
            injection_flags=flags,
            raw_payload={
                "provider": self.slug,
                "feed_url": self.feed_url,
                "entry_xml": ElementTree.tostring(entry, encoding="unicode")[:20000],
            },
        )

    @staticmethod
    def _link(entry: ElementTree.Element) -> str:
        rss_link = _text(entry.find("link"))
        if rss_link:
            return rss_link
        for link in entry.findall(f"{_ATOM}link"):
            rel = link.get("rel", "alternate")
            if rel == "alternate" and link.get("href"):
                return link.get("href", "")
        guid = entry.find("guid")
        # A guid is only a URL when the feed says so.
        if guid is not None and guid.get("isPermaLink", "true") == "true":
            return _text(guid)
        return ""

    @staticmethod
    def _images(entry: ElementTree.Element) -> tuple[str, ...]:
        """URLs only. Never fetched, never rehosted, never recreated (CLAUDE.md)."""
        urls = []
        for enclosure in entry.findall("enclosure"):
            if enclosure.get("type", "").startswith("image/") and enclosure.get("url"):
                urls.append(enclosure.get("url", ""))
        for media in entry.findall("{http://search.yahoo.com/mrss/}content"):
            if media.get("url"):
                urls.append(media.get("url", ""))
        return tuple(dict.fromkeys(urls))

    # ------------------------------------------------------------------ health
    def health(self) -> ProviderHealth:
        try:
            response = self._http().head(self.feed_url)
            ok = response.status_code < 400
            return ProviderHealth(
                ok=ok,
                detail=f"HTTP {response.status_code}",
                checked_at=datetime.now(UTC),
            )
        except httpx.HTTPError as exc:
            return ProviderHealth(ok=False, detail=str(exc), checked_at=datetime.now(UTC))
