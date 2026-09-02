"""Poll a provider, normalize, dedup, store.

The database-touching half of ingestion. `normalize` and `dedup` stay pure and are
tested without a database; everything that needs a session lives here.

Order matters and is cheapest-first (PIPELINE.md §4): a URL hash lookup costs an index
probe, a content hash costs another, and only what survives both is compared by SimHash.
Nothing here computes an embedding -- that is the desktop's job (ADR-0005), and a raw
article leaves this module with `embedding` null and `dedup_status` set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import BigInteger, Integer, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from thedrop_database.enums import CircuitState, DedupStatus, IngestStatus, SourceType
from thedrop_database.models import Provider, RawArticle, Source

from thedrop_ingest.dedup import NEAR_DUPLICATE_DISTANCE, bands, hamming_distance, simhash
from thedrop_ingest.normalize import NormalizedItem
from thedrop_ingest.providers import ProviderError, ProviderPage
from thedrop_ingest.providers.rss import RSSProvider

logger = logging.getLogger(__name__)

#: Circuit breaker (PIPELINE.md §2). Five consecutive failures opens the circuit for
#: fifteen minutes, then one probe is allowed through.
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_OPEN_DURATION = timedelta(minutes=15)

#: How far back to look when a provider has never succeeded. Bounded so a first poll of
#: a long-lived feed does not import a decade of archive.
FIRST_RUN_LOOKBACK = timedelta(days=2)

#: Candidate ceiling for the SimHash comparison. A band match is a prefilter, not a
#: guarantee; without a cap a popular band could pull thousands of rows into memory.
SIMHASH_CANDIDATE_LIMIT = 500

#: Allow-list of adapters, keyed by `providers.adapter_class`.
#:
#: DATABASE.md calls that column "dotted path resolved at runtime", and importlib on a
#: string from a table would turn any write access to `providers` into arbitrary code
#: execution. The registry keeps the column's meaning -- the value is still the dotted
#: path -- while making an unknown value a loud error instead of an import.
ADAPTER_REGISTRY: dict[str, type] = {
    "thedrop_ingest.providers.rss.RSSProvider": RSSProvider,
}


class AdapterNotRegisteredError(ProviderError):
    """`providers.adapter_class` names something not in ADAPTER_REGISTRY."""


def build_adapter(provider: Provider) -> RSSProvider:
    adapter_cls = ADAPTER_REGISTRY.get(provider.adapter_class)
    if adapter_cls is None:
        raise AdapterNotRegisteredError(
            f"adapter_class {provider.adapter_class!r} is not registered. "
            f"Known: {sorted(ADAPTER_REGISTRY)}"
        )

    config = provider.config or {}
    feed_url = config.get("feed_url")
    if not feed_url:
        raise ProviderError(f"provider {provider.slug!r} has no feed_url in config")
    return adapter_cls(slug=provider.slug, feed_url=feed_url)


# ------------------------------------------------------------------------ sources

#: Domains whose statements are primary by definition. This is a fact about the TLD,
#: not a judgement about credibility -- reliability_score stays at the model default
#: and is set by classification, never guessed here.
_AUTHORITY_SUFFIXES = (".gov", ".mil", ".gov.uk", ".europa.eu")


def resolve_source(db: Session, url: str) -> Source:
    """Find or create the `sources` row for a URL's domain.

    A new source starts untrusted (`allow_auto_publish=False`, type `unknown`, the
    model's default reliability). It can contribute context immediately but cannot
    satisfy a corroboration requirement alone until a human or a later phase classifies
    it -- inventing a reliability score here would put a number the pipeline trusts
    behind no evidence at all.
    """
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if not host:
        raise ProviderError(f"cannot determine a domain for {url!r}")

    source = db.scalar(select(Source).where(Source.domain == host))
    if source is not None:
        return source

    is_authority = host.endswith(_AUTHORITY_SUFFIXES)
    source = Source(
        domain=host,
        name=host,
        homepage_url=f"https://{host}/",
        source_type=SourceType.GOVERNMENT if is_authority else SourceType.UNKNOWN,
        is_primary_authority=is_authority,
        allow_auto_publish=False,
        reliability_basis={"origin": "auto-created on first ingest", "classified": False},
    )
    db.add(source)
    db.flush()
    logger.info("auto-created source", extra={"domain": host, "authority": is_authority})
    return source


# ------------------------------------------------------------------------- dedup


def classify_duplicate(db: Session, item: NormalizedItem) -> tuple[str, int | None]:
    """Run the cheap cascade. Returns (dedup_status, duplicate_of_id)."""
    existing = db.scalar(
        select(RawArticle.id).where(RawArticle.url_hash == item.url_hash).limit(1)
    )
    if existing is not None:
        return DedupStatus.EXACT_DUPLICATE, existing

    content = item.content_hash
    existing = db.scalar(
        select(RawArticle.id).where(RawArticle.content_hash == content).limit(1)
    )
    if existing is not None:
        # Identical syndication under a different URL -- invisible to the url_hash guard.
        return DedupStatus.EXACT_DUPLICATE, existing

    fingerprint = simhash(item.title, item.body_text)
    if fingerprint:
        near = _nearest_by_simhash(db, fingerprint)
        if near is not None:
            return DedupStatus.NEAR_DUPLICATE, near

    return DedupStatus.UNIQUE, None


def band_predicates(fingerprint: int) -> list[object]:
    """SQL for "shares at least one 16-bit band with `fingerprint`".

    The shift amount is bound as INTEGER, deliberately. Postgres defines the operator
    as `bigint >> integer`; letting SQLAlchemy infer BIGINT from the column produces
    `bigint >> bigint`, which does not exist and fails at execution with
    "operator does not exist". That is invisible in Python and only appears against a
    real server, which is why `test_band_predicates_compile_to_valid_postgres` asserts
    on the compiled SQL instead.
    """
    return [
        RawArticle.simhash.op(">>")(literal(i * 16, Integer)).op("&")(
            literal(0xFFFF, BigInteger)
        )
        == literal(band, BigInteger)
        for i, band in enumerate(bands(fingerprint))
    ]


def _nearest_by_simhash(db: Session, fingerprint: int) -> int | None:
    """Candidates share at least one 16-bit band, then are checked by Hamming distance.

    The band prefilter cannot produce a false negative while NEAR_DUPLICATE_DISTANCE is
    below the band count (pigeonhole), so this is an index-friendly narrowing rather
    than an approximation.
    """
    candidates = db.execute(
        select(RawArticle.id, RawArticle.simhash)
        .where(RawArticle.simhash.is_not(None), or_(*band_predicates(fingerprint)))
        .order_by(RawArticle.id.desc())
        .limit(SIMHASH_CANDIDATE_LIMIT)
    ).all()

    for candidate_id, candidate_hash in candidates:
        if hamming_distance(fingerprint, candidate_hash) <= NEAR_DUPLICATE_DISTANCE:
            return candidate_id
    return None


# ------------------------------------------------------------------------- store


def store_item(db: Session, provider: Provider, item: NormalizedItem) -> RawArticle | None:
    """Persist one normalized item. Returns None when it was a duplicate.

    Duplicates are stored, not discarded: knowing a story arrived from four sources is
    a signal clustering and corroboration both use. Only the dedup_status differs.
    """
    source = resolve_source(db, item.canonical_url)
    status, duplicate_of = classify_duplicate(db, item)

    published = datetime.fromisoformat(item.published_at_iso) if item.published_at_iso else None
    payload = dict(item.raw_payload)
    if item.timestamp_estimated:
        # The estimate is recorded where it cannot be mistaken for a reported time.
        payload["timestamp_estimated"] = True

    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=item.canonical_url,
        url_hash=item.url_hash,
        original_url=item.original_url,
        title=item.title,
        dek=item.dek,
        body_text=item.body_text,
        authors=list(item.authors),
        published_at=published or datetime.now(UTC),
        language=item.language,
        image_urls=list(item.image_urls),
        raw_payload=payload,
        simhash=simhash(item.title, item.body_text),
        content_hash=item.content_hash,
        injection_flags=dict(item.injection_flags),
        dedup_status=status,
        duplicate_of_id=duplicate_of,
        ingest_status=IngestStatus.NORMALIZED,
    )
    db.add(article)

    try:
        db.flush()
    except IntegrityError:
        # Two pollers raced on the same URL. The unique constraint is the authority;
        # losing the race is a duplicate, not an error.
        db.rollback()
        logger.debug("lost url_hash race", extra={"url": item.canonical_url})
        return None

    return article if status == DedupStatus.UNIQUE else None


# -------------------------------------------------------------------- the poll


def _circuit_allows(provider: Provider, now: datetime) -> bool:
    if provider.circuit_state != CircuitState.OPEN:
        return True
    opened = provider.circuit_opened_at
    if opened is None or now - opened >= CIRCUIT_OPEN_DURATION:
        # Half-open: exactly one probe. Success closes it, failure re-opens it.
        provider.circuit_state = CircuitState.HALF_OPEN
        return True
    return False


def _record_success(provider: Provider, page: ProviderPage, now: datetime) -> None:
    provider.consecutive_failures = 0
    provider.circuit_state = CircuitState.CLOSED
    provider.circuit_opened_at = None
    provider.last_success_at = now
    provider.last_error = None
    if page.next_cursor:
        provider.cursor = page.next_cursor


def _record_failure(provider: Provider, error: str, now: datetime) -> None:
    provider.consecutive_failures += 1
    provider.last_error_at = now
    provider.last_error = error[:2000]
    if provider.consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
        provider.circuit_state = CircuitState.OPEN
        provider.circuit_opened_at = now


def poll(db: Session, provider_slug: str) -> dict[str, object]:
    """Poll one provider and store what it returns.

    Never raises for a provider-side problem: a failing feed updates the breaker and
    reports, because one bad provider must not take down a scheduled task that polls
    the others.
    """
    now = datetime.now(UTC)
    provider = db.scalar(select(Provider).where(Provider.slug == provider_slug))

    if provider is None:
        return {"provider": provider_slug, "status": "unknown_provider"}
    if not provider.enabled:
        return {"provider": provider_slug, "status": "disabled"}
    if not _circuit_allows(provider, now):
        return {"provider": provider_slug, "status": "circuit_open"}

    since = (provider.last_success_at or now - FIRST_RUN_LOOKBACK).astimezone(UTC)

    try:
        adapter = build_adapter(provider)
        page = adapter.fetch(since, provider.cursor)
    except ProviderError as exc:
        _record_failure(provider, str(exc), now)
        db.commit()
        logger.warning(
            "provider poll failed",
            extra={"provider": provider_slug, "failures": provider.consecutive_failures},
        )
        return {"provider": provider_slug, "status": "error", "error": str(exc)}

    stored = duplicates = 0
    for item in page.items:
        if store_item(db, provider, item) is not None:
            stored += 1
        else:
            duplicates += 1

    _record_success(provider, page, now)
    db.commit()

    logger.info(
        "provider polled",
        extra={
            "provider": provider_slug,
            "fetched": len(page.items),
            "stored": stored,
            "duplicates": duplicates,
        },
    )
    return {
        "provider": provider_slug,
        "status": "ok",
        "fetched": len(page.items),
        "stored": stored,
        "duplicates": duplicates,
        "skipped": len(page.skipped),
    }
