"""Contradiction detection's VPS side: storing what the desktop posts (PIPELINE.md §11).

Mirrors tests/test_claims_extraction_db.py's approach -- call the endpoint function
directly with a FakeNode, no HTTP layer. What has to be right here:

  * one side authoritative + the other not -> the OTHER side becomes refuted, the
    authoritative claim is left untouched;
  * neither side authoritative, or both are -> both become disputed;
  * a claim touched by more than one contradicting pair keeps the more severe of the
    outcomes (refuted > disputed) regardless of the order the pairs arrive in;
  * `contradicted_by` accumulates every contradicting pair, even ones that did not
    decide the final status;
  * a story's contradictions_checked_at is set on every attempt, success or failure,
    same reasoning as claims_extracted_at;
  * a malformed or unknown claim id in a pair is skipped, not raised -- same bug class
    _parse_uuid already guards against for stories and for claim-evidence article ids.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.routers.worker import (
    ContradictionPairItem,
    ContradictionsRequest,
    StoryContradictions,
    store_contradictions,
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.enums import VerificationStatus
from thedrop_database.models import AiRun, Claim, Provider, RawArticle, Source, Story, StorySource

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-contradictions-fixture.invalid"
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
        slug="pytest-contradictions-provider",
        display_name="pytest",
        adapter_class="thedrop_ingest.providers.rss.RSSProvider",
        enabled=False,
        config={"feed_url": f"https://{TEST_DOMAIN}/feed.xml"},
    )
    db.add(created)
    db.flush()
    return created


@pytest.fixture
def source(db: Session) -> Source:
    created = Source(domain=TEST_DOMAIN, name="pytest contradictions fixture", country="US")
    db.add(created)
    db.flush()
    return created


@pytest.fixture
def story(db: Session, provider: Provider, source: Source) -> Story:
    url = f"https://{TEST_DOMAIN}/1"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(700_000).to_bytes(32, "big"),
        title="pytest contradictions fixture article",
        published_at=FIXTURE_EPOCH,
        discovered_at=FIXTURE_EPOCH,
        injection_flags={"patterns": []},
    )
    db.add(article)
    db.flush()
    created = Story(
        title="pytest contradictions fixture story",
        first_seen_at=FIXTURE_EPOCH,
        claims_extracted_at=FIXTURE_EPOCH,
    )
    db.add(created)
    db.flush()
    db.add(StorySource(story_id=created.id, raw_article_id=article.id, is_primary=True))
    article.story_id = created.id
    db.flush()
    return created


class FakeNode:
    id = 1
    name = "desktop-test"


def add_claim(
    db: Session,
    story: Story,
    *,
    n: int,
    verification_status: str = VerificationStatus.SINGLE_SOURCE,
    text: str | None = None,
) -> Claim:
    claim = Claim(
        story_id=story.id,
        claim_text=text or f"pytest claim {n}",
        claim_type="FACT",
        confidence=80,
        verification_status=verification_status,
    )
    db.add(claim)
    db.flush()
    return claim


def _pair(a: Claim, b: Claim, reason: str = "these cannot both be true") -> ContradictionPairItem:
    return ContradictionPairItem(
        claimIdA=str(a.public_id), claimIdB=str(b.public_id), reason=reason
    )


# ------------------------------------------------------------------ authoritative wins


def test_authoritative_claim_refutes_the_other_side(db: Session, story: Story) -> None:
    authoritative = add_claim(db, story, n=1, verification_status=VerificationStatus.AUTHORITATIVE)
    other = add_claim(db, story, n=2, verification_status=VerificationStatus.SINGLE_SOURCE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id),
                contradictions=[_pair(authoritative, other)],
            )
        ],
    )
    result = store_contradictions(request, FakeNode(), db)
    assert result["claimsFlagged"] == 1

    db.refresh(authoritative)
    db.refresh(other)
    assert authoritative.verification_status == VerificationStatus.AUTHORITATIVE
    assert other.verification_status == VerificationStatus.REFUTED
    assert other.contradicted_by == [
        {"claimId": str(authoritative.public_id), "reason": "these cannot both be true"}
    ]


def test_authoritative_side_is_untouched_regardless_of_pair_order(
    db: Session, story: Story
) -> None:
    """Same outcome whichever position the authoritative claim is passed in -- the
    decision is based on status, not on which side of the pair it happens to sit."""
    other = add_claim(db, story, n=1, verification_status=VerificationStatus.SINGLE_SOURCE)
    authoritative = add_claim(db, story, n=2, verification_status=VerificationStatus.AUTHORITATIVE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id),
                contradictions=[_pair(other, authoritative)],
            )
        ],
    )
    store_contradictions(request, FakeNode(), db)

    db.refresh(authoritative)
    db.refresh(other)
    assert authoritative.verification_status == VerificationStatus.AUTHORITATIVE
    assert other.verification_status == VerificationStatus.REFUTED


# --------------------------------------------------------------------------- disputed


def test_neither_authoritative_both_become_disputed(db: Session, story: Story) -> None:
    a = add_claim(db, story, n=1, verification_status=VerificationStatus.SINGLE_SOURCE)
    b = add_claim(db, story, n=2, verification_status=VerificationStatus.CORROBORATED)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[StoryContradictions(storyId=str(story.public_id), contradictions=[_pair(a, b)])],
    )
    store_contradictions(request, FakeNode(), db)

    db.refresh(a)
    db.refresh(b)
    assert a.verification_status == VerificationStatus.DISPUTED
    assert b.verification_status == VerificationStatus.DISPUTED
    assert a.contradicted_by == [
        {"claimId": str(b.public_id), "reason": "these cannot both be true"}
    ]
    assert b.contradicted_by == [
        {"claimId": str(a.public_id), "reason": "these cannot both be true"}
    ]


def test_both_authoritative_both_become_disputed(db: Session, story: Story) -> None:
    """Two authoritative sources disagreeing is not resolved by picking one -- both are
    flagged, and a human or a later signal decides."""
    a = add_claim(db, story, n=1, verification_status=VerificationStatus.AUTHORITATIVE)
    b = add_claim(db, story, n=2, verification_status=VerificationStatus.AUTHORITATIVE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[StoryContradictions(storyId=str(story.public_id), contradictions=[_pair(a, b)])],
    )
    store_contradictions(request, FakeNode(), db)

    db.refresh(a)
    db.refresh(b)
    assert a.verification_status == VerificationStatus.DISPUTED
    assert b.verification_status == VerificationStatus.DISPUTED


# ------------------------------------------------------------------------- accumulation


def test_severity_accumulates_refuted_wins_over_disputed(db: Session, story: Story) -> None:
    target = add_claim(db, story, n=1, verification_status=VerificationStatus.SINGLE_SOURCE)
    disputer = add_claim(db, story, n=2, verification_status=VerificationStatus.SINGLE_SOURCE)
    refuter = add_claim(db, story, n=3, verification_status=VerificationStatus.AUTHORITATIVE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id),
                contradictions=[
                    _pair(target, disputer, reason="disputed pair"),
                    _pair(refuter, target, reason="refuted pair"),
                ],
            )
        ],
    )
    store_contradictions(request, FakeNode(), db)

    db.refresh(target)
    assert target.verification_status == VerificationStatus.REFUTED
    reasons = {entry["reason"] for entry in target.contradicted_by}
    assert reasons == {"disputed pair", "refuted pair"}


def test_severity_accumulates_regardless_of_arrival_order(db: Session, story: Story) -> None:
    """The refuted outcome must win even when the pair that produces it is processed
    first -- severity comparison, not last-write-wins."""
    target = add_claim(db, story, n=1, verification_status=VerificationStatus.SINGLE_SOURCE)
    disputer = add_claim(db, story, n=2, verification_status=VerificationStatus.SINGLE_SOURCE)
    refuter = add_claim(db, story, n=3, verification_status=VerificationStatus.AUTHORITATIVE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id),
                contradictions=[
                    _pair(refuter, target, reason="refuted pair"),
                    _pair(target, disputer, reason="disputed pair"),
                ],
            )
        ],
    )
    store_contradictions(request, FakeNode(), db)

    db.refresh(target)
    assert target.verification_status == VerificationStatus.REFUTED


# -------------------------------------------------------------------- id resolution


def test_a_pair_with_a_malformed_claim_id_is_skipped(db: Session, story: Story) -> None:
    a = add_claim(db, story, n=1, verification_status=VerificationStatus.SINGLE_SOURCE)
    b = add_claim(db, story, n=2, verification_status=VerificationStatus.SINGLE_SOURCE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id),
                contradictions=[
                    ContradictionPairItem(
                        claimIdA="not-a-uuid", claimIdB=str(b.public_id), reason="x"
                    ),
                    _pair(a, b, reason="a real pair"),
                ],
            )
        ],
    )
    result = store_contradictions(request, FakeNode(), db)

    assert result["claimsFlagged"] == 2
    db.refresh(a)
    db.refresh(b)
    assert a.verification_status == VerificationStatus.DISPUTED
    assert b.verification_status == VerificationStatus.DISPUTED
    assert len(b.contradicted_by) == 1


def test_a_pair_with_an_unknown_claim_id_is_skipped(db: Session, story: Story) -> None:
    a = add_claim(db, story, n=1, verification_status=VerificationStatus.SINGLE_SOURCE)

    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id),
                contradictions=[
                    ContradictionPairItem(
                        claimIdA=str(a.public_id),
                        claimIdB="00000000-0000-0000-0000-000000000000",
                        reason="x",
                    )
                ],
            )
        ],
    )
    result = store_contradictions(request, FakeNode(), db)

    assert result["claimsFlagged"] == 0
    db.refresh(a)
    assert a.verification_status == VerificationStatus.SINGLE_SOURCE


def test_an_unknown_story_id_is_reported_not_raised(db: Session) -> None:
    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId="00000000-0000-0000-0000-000000000000",
                contradictions=[],
            )
        ],
    )
    result = store_contradictions(request, FakeNode(), db)
    assert result["unknown"] == ["00000000-0000-0000-0000-000000000000"]
    assert result["stored"] == 0


def test_a_malformed_story_id_is_reported_not_raised(db: Session) -> None:
    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[StoryContradictions(storyId="not-a-uuid-either", contradictions=[])],
    )
    result = store_contradictions(request, FakeNode(), db)
    assert result["unknown"] == ["not-a-uuid-either"]
    assert result["stored"] == 0


# --------------------------------------------------------------------------- checked_at


def test_contradictions_checked_at_is_set_on_success(db: Session, story: Story) -> None:
    a = add_claim(db, story, n=1)
    b = add_claim(db, story, n=2)
    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id), contradictions=[], injectionDetected=True
            )
        ],
    )
    store_contradictions(request, FakeNode(), db)

    db.refresh(story)
    assert story.contradictions_checked_at is not None
    # Untouched by an item with no pairs.
    db.refresh(a)
    db.refresh(b)
    assert a.verification_status == VerificationStatus.SINGLE_SOURCE


def test_contradictions_checked_at_is_set_on_failure(db: Session, story: Story) -> None:
    request = ContradictionsRequest(
        model="qwen2.5:7b",
        items=[
            StoryContradictions(
                storyId=str(story.public_id), error="invalid output after one retry"
            )
        ],
    )
    result = store_contradictions(request, FakeNode(), db)

    assert result["failed"] == 1
    db.refresh(story)
    assert story.contradictions_checked_at is not None

    run = db.scalar(select(AiRun).where(AiRun.story_id == story.id))
    assert run is not None
    assert run.status == "invalid_output"
    assert run.provider == "ollama"


def test_provider_is_inferred_from_the_model_name(db: Session, story: Story) -> None:
    request = ContradictionsRequest(
        model="claude-haiku-4-5-20251001",
        items=[StoryContradictions(storyId=str(story.public_id), contradictions=[])],
    )
    store_contradictions(request, FakeNode(), db)

    run = db.scalar(select(AiRun).where(AiRun.story_id == story.id))
    assert run is not None
    assert run.provider == "anthropic"
