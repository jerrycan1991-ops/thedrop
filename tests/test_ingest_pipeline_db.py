"""The ingestion paths that need a real Postgres.

Every test here covers something that has **already broken in production**, or that
could only break against a live server:

  * `operator does not exist: bigint >> bigint` -- the band prefilter failed on the
    first real poll. It is invisible in Python and invisible in the compiled SQL.
  * `since` resuming exactly at `last_success_at`, which silently and permanently lost
    articles to feed lag. The second real poll returned `fetched: 0, skipped: 10`.

Neither was caught by 180-odd passing tests, because both live in the gap between
Python and Postgres. Marked `db` and skipped when no database is reachable
(tests/conftest.py probes DATABASE_URL, so a managed provider counts).

Every test rolls back. Nothing here commits, so it is safe against the live database --
which is the only database available under ADR-0012.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database.enums import DedupStatus, SourceType
from thedrop_database.models import Provider, RawArticle
from thedrop_ingest.dedup import simhash
from thedrop_ingest.normalize import NormalizedItem
from thedrop_ingest.pipeline import (
    classify_duplicate,
    resolve_source,
    store_item,
)

pytestmark = pytest.mark.db

#: Domain used by every fixture item. Distinctive so a stray committed row would be
#: obvious rather than blending into real ingested data.
TEST_DOMAIN = "pytest-fixture.invalid"


@pytest.fixture
def db() -> Iterator[Session]:
    """A session that is always rolled back.

    Deliberately not `session_scope`, which commits. These run against the production
    database because it is the only Postgres available (ADR-0012), so committing would
    put fixture rows in the live `raw_articles` table.
    """
    from thedrop_database.session import get_engine

    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def provider(db: Session) -> Provider:
    existing = db.scalar(select(Provider).limit(1))
    if existing is not None:
        return existing
    created = Provider(
        slug="pytest-provider",
        display_name="pytest",
        adapter_class="thedrop_ingest.providers.rss.RSSProvider",
        enabled=False,
        config={"feed_url": "https://pytest-fixture.invalid/feed.xml"},
    )
    db.add(created)
    db.flush()
    return created


def item(
    *,
    path: str = "/a",
    title: str = "Senate passes budget bill",
    body: str = "The Senate approved the measure 51-49 on Tuesday evening.",
) -> NormalizedItem:
    url = f"https://{TEST_DOMAIN}{path}"
    return NormalizedItem(
        canonical_url=url,
        original_url=url,
        title=title,
        body_text=body,
        published_at_iso=datetime.now(UTC).isoformat(),
        timestamp_estimated=False,
        injection_flags={"patterns": []},
        raw_payload={"provider": "pytest"},
    )


# ------------------------------------------------------------------ band predicates


def test_simhash_band_query_executes_against_postgres(db: Session) -> None:
    """The regression for `operator does not exist: bigint >> bigint`.

    Postgres defines the operator as `bigint >> integer`. Inferring the right operand's
    type from the column gives `bigint >> bigint`, which does not exist -- and the
    failure appears only here, at execution, against a real server.
    """
    from thedrop_ingest.pipeline import _nearest_by_simhash

    # Executing at all is the assertion; an empty result is a fine outcome.
    assert _nearest_by_simhash(db, simhash("Senate passes budget bill", "Approved 51-49.")) is None


def test_band_query_tolerates_the_extreme_simhash_values(db: Session) -> None:
    """The sign bit is where a bigint round-trip goes wrong.

    SimHash is stored signed because Postgres has no unsigned bigint, so the largest
    and smallest representable values are the ones most likely to break the shift.
    """
    from thedrop_ingest.pipeline import _nearest_by_simhash

    for value in (-(2**63), 2**63 - 1, -1, 0):
        assert _nearest_by_simhash(db, value) is None


# ------------------------------------------------------------------ dedup cascade


def test_first_sighting_is_unique(db: Session, provider: Provider) -> None:
    status, duplicate_of = classify_duplicate(db, item())

    assert status == DedupStatus.UNIQUE
    assert duplicate_of is None


def test_same_url_is_an_exact_duplicate(db: Session, provider: Provider) -> None:
    """Check 1 of the cascade: the unique constraint on url_hash."""
    stored = store_item(db, provider, item())
    assert stored is not None

    status, duplicate_of = classify_duplicate(db, item())

    assert status == DedupStatus.EXACT_DUPLICATE
    assert duplicate_of == stored.id


def test_same_body_under_a_different_url_is_an_exact_duplicate(
    db: Session, provider: Provider
) -> None:
    """Check 2: identical syndication the url_hash guard cannot see."""
    stored = store_item(db, provider, item(path="/original"))
    assert stored is not None

    status, duplicate_of = classify_duplicate(db, item(path="/syndicated"))

    assert status == DedupStatus.EXACT_DUPLICATE
    assert duplicate_of == stored.id


def test_reworded_headline_is_a_near_duplicate(db: Session, provider: Provider) -> None:
    """Check 3: SimHash within Hamming distance 3, evaluated through the band query."""
    stored = store_item(
        db,
        provider,
        item(path="/wire", title="Senate passes budget bill after late-night vote"),
    )
    assert stored is not None

    status, duplicate_of = classify_duplicate(
        db,
        item(
            path="/rewrite",
            title="Senate passes budget bill after late night vote",
            body="The Senate approved the measure 51-49 on Tuesday evening!",
        ),
    )

    assert status == DedupStatus.NEAR_DUPLICATE
    assert duplicate_of == stored.id


def test_a_different_story_is_not_a_duplicate(db: Session, provider: Provider) -> None:
    """The cascade must not merge unrelated stories; that would lose coverage silently."""
    assert store_item(db, provider, item(path="/senate")) is not None

    status, _ = classify_duplicate(
        db,
        item(
            path="/hurricane",
            title="Hurricane makes landfall in Florida",
            body="Wind speeds reached 120 mph as the storm came ashore near Tampa.",
        ),
    )

    assert status == DedupStatus.UNIQUE


# ------------------------------------------------------------------ storage


def test_store_returns_none_for_a_duplicate_but_still_records_it(
    db: Session, provider: Provider
) -> None:
    """Duplicates are stored, not discarded.

    That a story arrived from four sources is a signal clustering and corroboration
    both use; only dedup_status differs.
    """
    assert store_item(db, provider, item()) is not None
    assert store_item(db, provider, item(path="/elsewhere")) is None

    rows = db.scalars(
        select(RawArticle).where(RawArticle.canonical_url.like(f"https://{TEST_DOMAIN}%"))
    ).all()
    statuses = {r.dedup_status for r in rows}

    assert len(rows) == 2
    assert DedupStatus.EXACT_DUPLICATE in statuses


def test_stored_row_round_trips_every_column_type(db: Session, provider: Provider) -> None:
    """ARRAY, JSONB, bytea and a signed bigint all have to survive the driver."""
    stored = store_item(
        db,
        provider,
        NormalizedItem(
            canonical_url=f"https://{TEST_DOMAIN}/types",
            original_url=f"https://{TEST_DOMAIN}/types?utm_source=x",
            title="Column types",
            body_text="Body text for the hash.",
            published_at_iso=datetime.now(UTC).isoformat(),
            timestamp_estimated=True,
            authors=("A. Reporter", "B. Writer"),
            image_urls=(f"https://{TEST_DOMAIN}/a.jpg",),
            injection_flags={"patterns": ["ignore_previous"], "invisible_chars": 2},
            raw_payload={"nested": {"provider": "pytest"}},
        ),
    )
    assert stored is not None
    db.expire(stored)

    assert stored.authors == ["A. Reporter", "B. Writer"]
    assert stored.image_urls == [f"https://{TEST_DOMAIN}/a.jpg"]
    assert stored.injection_flags["patterns"] == ["ignore_previous"]
    assert stored.raw_payload["nested"]["provider"] == "pytest"
    # Recorded where it cannot be mistaken for a reported publication time.
    assert stored.raw_payload["timestamp_estimated"] is True
    assert len(stored.url_hash) == 32
    assert -(2**63) <= stored.simhash < 2**63
    assert stored.embedding is None, "the VPS must never compute an embedding"


def test_embedding_is_left_for_the_desktop(db: Session, provider: Provider) -> None:
    """ADR-0005: embeddings are computed on the desktop, never on the VPS."""
    stored = store_item(db, provider, item(path="/no-embedding"))

    assert stored is not None
    assert stored.embedding is None
    assert stored.embedded_at is None


# ------------------------------------------------------------------ source policy


def test_new_domain_is_auto_created_untrusted(db: Session) -> None:
    source = resolve_source(db, f"https://{TEST_DOMAIN}/story")

    assert source.domain == TEST_DOMAIN
    assert source.source_type == SourceType.UNKNOWN
    assert source.is_primary_authority is False
    # The guard that stops an unclassified publisher satisfying corroboration alone.
    assert source.allow_auto_publish is False


def test_www_is_stripped_so_one_publisher_is_one_source(db: Session) -> None:
    bare = resolve_source(db, f"https://{TEST_DOMAIN}/a")
    prefixed = resolve_source(db, f"https://www.{TEST_DOMAIN}/b")

    assert bare.id == prefixed.id


def test_government_domains_are_marked_primary_authority(db: Session) -> None:
    """A fact about the TLD. Reliability stays at the default and is never guessed."""
    source = resolve_source(db, "https://www.federalreserve.gov/news/a.htm")

    assert source.is_primary_authority is True
    assert source.source_type == SourceType.GOVERNMENT
    assert source.allow_auto_publish is False


# ------------------------------------------------------------------ poll window


def test_poll_window_overlap_would_have_caught_the_lost_articles(
    db: Session, provider: Provider
) -> None:
    """The regression for `fetched: 0, skipped: 10`.

    An item published just before the previous poll, but only appearing in the feed
    afterwards, must still fall inside the window. Resuming exactly at
    `last_success_at` excluded it forever.
    """
    from thedrop_ingest.pipeline import window_start

    now = datetime.now(UTC)
    last_poll = now - timedelta(minutes=15)
    published_just_before_that_poll = last_poll - timedelta(minutes=2)

    assert window_start(last_poll, now) <= published_just_before_that_poll
