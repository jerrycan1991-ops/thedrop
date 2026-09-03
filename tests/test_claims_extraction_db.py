"""Claim extraction's VPS side: storing what the desktop posts (PIPELINE.md §10-11).

Mirrors tests/test_entity_extraction_db.py's approach -- call the endpoint function
directly with a FakeNode, no HTTP layer. What has to be right here:

  * a story's claims_extracted_at is set on every attempt, success or failure, or a
    consistently-failing story would be re-dispatched forever with no record it was
    ever tried (see the migration that added the column);
  * a claim whose evidence cites only unknown articles is dropped, not stored with no
    evidence -- an evidence-less claim is unauditable by design;
  * re-extraction REPLACES a story's claim set, same reasoning as store_entities;
  * a failed item does not touch the story's existing (successful) claims.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.routers.worker import (
    ClaimsRequest,
    ExtractedClaimEvidence,
    ExtractedClaimItem,
    StoryClaims,
    store_claims,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.models import (
    AiRun,
    Claim,
    ClaimEvidence,
    Entity,
    Provider,
    RawArticle,
    Source,
    Story,
    StorySource,
)

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-claims-extract-fixture.invalid"
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
        slug="pytest-claims-extract-provider",
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
    created = Source(domain=TEST_DOMAIN, name="pytest claims extract fixture", country="US")
    db.add(created)
    db.flush()
    return created


def add_article(db: Session, provider: Provider, source: Source, *, n: int) -> RawArticle:
    url = f"https://{TEST_DOMAIN}/{n}"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(600_000 + n).to_bytes(32, "big"),
        title=f"pytest claims extract fixture {n}",
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
    )
    db.add(article)
    db.flush()
    return article


def add_story(db: Session, *members: RawArticle) -> Story:
    story = Story(title="pytest claims extract fixture story", first_seen_at=FIXTURE_EPOCH)
    db.add(story)
    db.flush()
    for article in members:
        db.add(StorySource(story_id=story.id, raw_article_id=article.id, is_primary=True))
        article.story_id = story.id
    db.flush()
    return story


class FakeNode:
    id = 1
    name = "desktop-test"


def _claim_item(
    text: str = "The bridge will close for repairs.",
    claim_type: str = "FACT",
    attributed_to: str | None = None,
    *,
    evidence: list[ExtractedClaimEvidence] | None = None,
) -> ExtractedClaimItem:
    return ExtractedClaimItem(
        claim_text=text,
        claim_type=claim_type,
        attributed_to=attributed_to,
        confidence=90,
        evidence=evidence or [ExtractedClaimEvidence(source_article_id="placeholder", quote="q")],
    )


# ------------------------------------------------------------------------- happy path


def test_a_successful_extraction_stores_claims_and_evidence(
    db: Session, provider: Provider, source: Source
) -> None:
    article = add_article(db, provider, source, n=1)
    story = add_story(db, article)

    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(
                storyId=str(story.public_id),
                claims=[
                    _claim_item(
                        evidence=[
                            ExtractedClaimEvidence(
                                source_article_id=str(article.public_id), quote="the bridge closes"
                            )
                        ]
                    )
                ],
                injectionDetected=False,
                riskTier="standard",
                riskReasons=[],
            )
        ],
    )

    result = store_claims(request, FakeNode(), db)
    assert result == {"stored": 1, "claims": 1, "failed": 0, "unknown": []}

    claim = db.scalar(select(Claim).where(Claim.story_id == story.id))
    assert claim is not None
    assert claim.claim_text == "The bridge will close for repairs."
    assert claim.supporting_source_count == 1

    evidence = db.scalars(select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)).all()
    assert len(evidence) == 1
    assert evidence[0].raw_article_id == article.id
    assert evidence[0].source_id == source.id
    assert evidence[0].stance == "supports"

    db.refresh(story)
    assert story.claims_extracted_at is not None
    assert story.risk_tier == "standard"


def test_evidence_from_multiple_articles_counts_distinct_sources(
    db: Session, provider: Provider, source: Source
) -> None:
    other_source = Source(domain="pytest-claims-other.invalid", name="other", country="GB")
    db.add(other_source)
    db.flush()

    article_a = add_article(db, provider, source, n=10)
    article_b = add_article(db, provider, other_source, n=11)
    story = add_story(db, article_a, article_b)

    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(
                storyId=str(story.public_id),
                claims=[
                    _claim_item(
                        evidence=[
                            ExtractedClaimEvidence(
                                source_article_id=str(article_a.public_id), quote="a"
                            ),
                            ExtractedClaimEvidence(
                                source_article_id=str(article_b.public_id), quote="b"
                            ),
                        ]
                    )
                ],
                riskTier="standard",
            )
        ],
    )

    store_claims(request, FakeNode(), db)

    claim = db.scalar(select(Claim).where(Claim.story_id == story.id))
    assert claim is not None
    assert claim.supporting_source_count == 2
    evidence = db.scalars(select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)).all()
    assert len(evidence) == 2


def test_attribution_resolves_to_an_other_typed_entity(
    db: Session, provider: Provider, source: Source
) -> None:
    article = add_article(db, provider, source, n=20)
    story = add_story(db, article)

    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(
                storyId=str(story.public_id),
                claims=[
                    _claim_item(
                        text="The mayor confirmed the closure.",
                        claim_type="OFFICIAL_STATEMENT",
                        attributed_to="Mayor Elena Ruiz",
                        evidence=[
                            ExtractedClaimEvidence(
                                source_article_id=str(article.public_id), quote="q"
                            )
                        ],
                    )
                ],
                riskTier="standard",
            )
        ],
    )
    store_claims(request, FakeNode(), db)

    claim = db.scalar(select(Claim).where(Claim.story_id == story.id))
    assert claim is not None
    entity = db.get(Entity, claim.attributed_to_entity_id)
    assert entity is not None
    assert entity.canonical_name == "Mayor Elena Ruiz"
    assert entity.entity_type == "OTHER"


# --------------------------------------------------------------- evidence resolution


def test_a_claim_citing_only_a_malformed_article_id_is_dropped(
    db: Session, provider: Provider, source: Source
) -> None:
    """Also the regression test for the bug this endpoint had while it was being
    written: comparing a UUID column against a non-UUID string raised a raw DB error
    and aborted the whole request's transaction, rather than being treated as "not
    found" like an unknown-but-well-formed id already is. source_article_id is model
    output, not a guaranteed-well-formed system value, so this is a real input shape,
    not just a test artifact."""
    article = add_article(db, provider, source, n=30)
    story = add_story(db, article)

    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(
                storyId=str(story.public_id),
                claims=[
                    _claim_item(
                        evidence=[ExtractedClaimEvidence(source_article_id="not-a-uuid", quote="q")]
                    )
                ],
                riskTier="standard",
            )
        ],
    )
    result = store_claims(request, FakeNode(), db)

    assert result["claims"] == 0
    assert db.scalar(select(Claim).where(Claim.story_id == story.id)) is None
    # The story is still marked as attempted -- the claim was dropped, not the whole item.
    db.refresh(story)
    assert story.claims_extracted_at is not None


# --------------------------------------------------------------------------- replace


def test_re_extraction_replaces_the_previous_claim_set(
    db: Session, provider: Provider, source: Source
) -> None:
    article = add_article(db, provider, source, n=40)
    story = add_story(db, article)

    def request(text: str) -> ClaimsRequest:
        return ClaimsRequest(
            model="qwen2.5:7b",
            items=[
                StoryClaims(
                    storyId=str(story.public_id),
                    claims=[
                        _claim_item(
                            text=text,
                            evidence=[
                                ExtractedClaimEvidence(
                                    source_article_id=str(article.public_id), quote="q"
                                )
                            ],
                        )
                    ],
                    riskTier="standard",
                )
            ],
        )

    store_claims(request("first extraction"), FakeNode(), db)
    store_claims(request("second extraction"), FakeNode(), db)

    remaining = db.scalars(select(Claim).where(Claim.story_id == story.id)).all()
    assert len(remaining) == 1
    assert remaining[0].claim_text == "second extraction"


# ------------------------------------------------------------------------- failures


def test_a_failed_item_records_an_ai_run_but_no_claims(
    db: Session, provider: Provider, source: Source
) -> None:
    article = add_article(db, provider, source, n=50)
    story = add_story(db, article)

    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[StoryClaims(storyId=str(story.public_id), error="invalid output after one retry")],
    )
    result = store_claims(request, FakeNode(), db)

    assert result == {"stored": 1, "claims": 0, "failed": 1, "unknown": []}
    assert db.scalar(select(Claim).where(Claim.story_id == story.id)) is None

    run = db.scalar(select(AiRun).where(AiRun.story_id == story.id))
    assert run is not None
    assert run.status == "invalid_output"
    assert run.provider == "ollama"

    db.refresh(story)
    assert story.claims_extracted_at is not None


def test_a_failed_item_does_not_delete_previously_stored_claims(
    db: Session, provider: Provider, source: Source
) -> None:
    """A retry that fails must not destroy a prior success -- extraction can be
    re-run for many reasons, and a transient failure should not regress a story that
    already has good claims."""
    article = add_article(db, provider, source, n=60)
    story = add_story(db, article)

    success = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(
                storyId=str(story.public_id),
                claims=[
                    _claim_item(
                        evidence=[
                            ExtractedClaimEvidence(
                                source_article_id=str(article.public_id), quote="q"
                            )
                        ]
                    )
                ],
                riskTier="standard",
            )
        ],
    )
    store_claims(success, FakeNode(), db)

    failure = ClaimsRequest(
        model="qwen2.5:7b",
        items=[StoryClaims(storyId=str(story.public_id), error="invalid output after one retry")],
    )
    store_claims(failure, FakeNode(), db)

    remaining = db.scalars(select(Claim).where(Claim.story_id == story.id)).all()
    assert len(remaining) == 1


def test_an_unknown_story_id_is_reported_not_raised(db: Session) -> None:
    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(
                storyId="00000000-0000-0000-0000-000000000000",
                claims=[_claim_item()],
                riskTier="standard",
            )
        ],
    )
    result = store_claims(request, FakeNode(), db)
    assert result["unknown"] == ["00000000-0000-0000-0000-000000000000"]
    assert result["stored"] == 0


def test_a_malformed_story_id_is_reported_not_raised(db: Session) -> None:
    """Same bug class as the malformed-article-id test, at the story lookup instead
    of the evidence lookup -- this one runs first, so getting it wrong would abort
    the whole batch's transaction before any item is processed."""
    request = ClaimsRequest(
        model="qwen2.5:7b",
        items=[
            StoryClaims(storyId="not-a-uuid-either", claims=[_claim_item()], riskTier="standard")
        ],
    )
    result = store_claims(request, FakeNode(), db)
    assert result["unknown"] == ["not-a-uuid-either"]
    assert result["stored"] == 0


# -------------------------------------------------------------- request validation


def test_a_successful_item_without_risk_tier_is_rejected() -> None:
    with pytest.raises(ValidationError, match="risk_tier is required"):
        ClaimsRequest(
            model="qwen2.5:7b",
            items=[StoryClaims(storyId="x", claims=[_claim_item()])],
        )


def test_a_failed_item_needs_no_risk_tier() -> None:
    ClaimsRequest(
        model="qwen2.5:7b",
        items=[StoryClaims(storyId="x", error="boom")],
    )  # must not raise


# ------------------------------------------------------------------------- provider


def test_provider_is_inferred_from_the_model_name(
    db: Session, provider: Provider, source: Source
) -> None:
    article = add_article(db, provider, source, n=70)
    story = add_story(db, article)

    request = ClaimsRequest(
        model="claude-haiku-4-5-20251001",
        items=[
            StoryClaims(
                storyId=str(story.public_id),
                claims=[
                    _claim_item(
                        evidence=[
                            ExtractedClaimEvidence(
                                source_article_id=str(article.public_id), quote="q"
                            )
                        ]
                    )
                ],
                riskTier="standard",
            )
        ],
    )
    store_claims(request, FakeNode(), db)

    run = db.scalar(select(AiRun).where(AiRun.story_id == story.id))
    assert run is not None
    assert run.provider == "anthropic"
