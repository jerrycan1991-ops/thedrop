"""Cross-source verification against real rows (PIPELINE.md §11).

Exercises verify_claim/unverified_claim_ids end to end -- compute_status itself is
covered without a database in test_verification.py. What has to be right here is the
wiring: the right columns get joined, the write actually lands, and the dispatch
query only ever offers a claim once.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.models import (
    Claim,
    ClaimEvidence,
    Provider,
    RawArticle,
    Source,
    Story,
)
from thedrop_database.verification import unverified_claim_ids, verify_claim

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-verify-fixture.invalid"
FIXTURE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def db() -> Iterator[Session]:
    connection = engine().connect()
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
        slug="pytest-verify-provider",
        display_name="pytest",
        adapter_class="thedrop_ingest.providers.rss.RSSProvider",
        enabled=False,
        config={"feed_url": f"https://{TEST_DOMAIN}/feed.xml"},
    )
    db.add(created)
    db.flush()
    return created


@pytest.fixture
def story(db: Session) -> Story:
    row = Story(title="pytest verify fixture story", first_seen_at=FIXTURE_EPOCH)
    db.add(row)
    db.flush()
    return row


def source(db: Session, domain: str, *, is_primary_authority: bool = False) -> Source:
    row = Source(domain=domain, name=domain, is_primary_authority=is_primary_authority)
    db.add(row)
    db.flush()
    return row


def article(
    db: Session, provider: Provider, src: Source, n: int, *, content: bytes | None = None
) -> RawArticle:
    url = f"https://{src.domain}/{n}"
    row = RawArticle(
        provider_id=provider.id,
        source_id=src.id,
        canonical_url=url,
        original_url=url,
        url_hash=(950_000 + n).to_bytes(32, "big"),
        title=f"pytest verify fixture {n}",
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
        content_hash=content,
    )
    db.add(row)
    db.flush()
    return row


def claim_with_evidence(
    db: Session, story: Story, evidence: list[tuple[RawArticle, Source]]
) -> Claim:
    row = Claim(
        story_id=story.id,
        claim_text="pytest claim text",
        claim_type="FACT",
        confidence=80,
    )
    db.add(row)
    db.flush()
    for art, src in evidence:
        db.add(
            ClaimEvidence(
                claim_id=row.id,
                raw_article_id=art.id,
                source_id=src.id,
                quote="pytest quote",
                url=art.canonical_url,
                stance="supports",
            )
        )
    db.flush()
    return row


def test_a_claim_with_no_evidence_stays_unverified(db: Session, story: Story) -> None:
    claim = Claim(story_id=story.id, claim_text="x", claim_type="FACT", confidence=50)
    db.add(claim)
    db.flush()

    status = verify_claim(db, claim.id)

    assert status == "unverified"
    db.refresh(claim)
    assert claim.verification_status == "unverified"
    assert claim.verified_at is not None, "an attempt was made, even if inconclusive"


def test_a_single_source_claim_is_marked_single_source(
    db: Session, provider: Provider, story: Story
) -> None:
    src = source(db, "pytest-verify-single.invalid")
    art = article(db, provider, src, 1, content=b"hash-a")
    claim = claim_with_evidence(db, story, [(art, src)])

    status = verify_claim(db, claim.id)

    assert status == "single_source"
    db.refresh(claim)
    assert claim.verification_status == "single_source"


def test_two_independent_sources_corroborate(db: Session, provider: Provider, story: Story) -> None:
    src_a = source(db, "pytest-verify-a.invalid")
    src_b = source(db, "pytest-verify-b.invalid")
    art_a = article(db, provider, src_a, 10, content=b"hash-a")
    art_b = article(db, provider, src_b, 11, content=b"hash-b")
    claim = claim_with_evidence(db, story, [(art_a, src_a), (art_b, src_b)])

    status = verify_claim(db, claim.id)

    assert status == "corroborated"


def test_syndicated_copies_do_not_corroborate(
    db: Session, provider: Provider, story: Story
) -> None:
    """Two outlets, same wire content_hash -- ADR-0013's "one witness under two
    mastheads", reproduced with real rows."""
    src_a = source(db, "pytest-verify-wire-a.invalid")
    src_b = source(db, "pytest-verify-wire-b.invalid")
    shared_wire_copy = b"identical-wire-body"
    art_a = article(db, provider, src_a, 20, content=shared_wire_copy)
    art_b = article(db, provider, src_b, 21, content=shared_wire_copy)
    claim = claim_with_evidence(db, story, [(art_a, src_a), (art_b, src_b)])

    status = verify_claim(db, claim.id)

    assert status == "single_source"


def test_an_authoritative_source_is_marked_authoritative(
    db: Session, provider: Provider, story: Story
) -> None:
    gov = source(db, "pytest-verify.gov", is_primary_authority=True)
    art = article(db, provider, gov, 30, content=b"hash-gov")
    claim = claim_with_evidence(db, story, [(art, gov)])

    status = verify_claim(db, claim.id)

    assert status == "authoritative"


def test_unverified_claim_ids_excludes_already_verified_claims(
    db: Session, provider: Provider, story: Story
) -> None:
    src = source(db, "pytest-verify-dispatch.invalid")
    art = article(db, provider, src, 40, content=b"hash-d")
    claim = claim_with_evidence(db, story, [(art, src)])

    assert claim.id in unverified_claim_ids(db, limit=1000)

    verify_claim(db, claim.id)

    assert claim.id not in unverified_claim_ids(db, limit=1000)
