"""claims, claim_evidence, ai_runs and prompt_versions -- the schema, not the
extraction pipeline that will populate it (PIPELINE.md §10-11, DATABASE.md §9).

What has to be right here is the invariant the schema itself is supposed to enforce,
not application logic: the attribution CHECK constraint, the one-active-prompt-per-name
partial index, and that cascades take evidence and provenance down with the rows they
describe rather than leaving orphans.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.models import (
    AiRun,
    Claim,
    ClaimEvidence,
    Entity,
    PromptVersion,
    Provider,
    RawArticle,
    Source,
    Story,
)

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-claims-fixture.invalid"
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
        slug="pytest-claims-provider",
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
    row = Source(domain=TEST_DOMAIN, name=TEST_DOMAIN, country="US")
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def story(db: Session) -> Story:
    row = Story(title="pytest claims fixture story", first_seen_at=FIXTURE_EPOCH)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def article(db: Session, provider: Provider, source: Source) -> RawArticle:
    url = f"https://{source.domain}/1"
    row = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(700_001).to_bytes(32, "big"),
        title="pytest claims fixture article",
        published_at=FIXTURE_EPOCH,
        discovered_at=FIXTURE_EPOCH,
        injection_flags={"patterns": []},
    )
    db.add(row)
    db.flush()
    return row


def _claim(story: Story, claim_type: str = "FACT", **overrides: object) -> Claim:
    defaults = {
        "story_id": story.id,
        "claim_text": "pytest claim text",
        "claim_type": claim_type,
        "confidence": 80,
    }
    defaults.update(overrides)
    return Claim(**defaults)


# --------------------------------------------------------------- attribution CHECK


def test_a_fact_needs_no_attribution(db: Session, story: Story) -> None:
    db.add(_claim(story, claim_type="FACT"))
    db.flush()  # must not raise


def test_a_claim_type_without_attribution_is_rejected(db: Session, story: Story) -> None:
    """PIPELINE.md §11: 'Person X claims Y' must never silently become 'Y happened' --
    this is the schema refusing to let extraction lose who said it."""
    db.add(_claim(story, claim_type="CLAIM", attributed_to_entity_id=None))
    with pytest.raises(IntegrityError, match="ck_claims_attribution_required"):
        db.flush()


def test_an_allegation_without_attribution_is_rejected(db: Session, story: Story) -> None:
    db.add(_claim(story, claim_type="ALLEGATION", attributed_to_entity_id=None))
    with pytest.raises(IntegrityError, match="ck_claims_attribution_required"):
        db.flush()


def test_an_official_statement_without_attribution_is_rejected(db: Session, story: Story) -> None:
    db.add(_claim(story, claim_type="OFFICIAL_STATEMENT", attributed_to_entity_id=None))
    with pytest.raises(IntegrityError, match="ck_claims_attribution_required"):
        db.flush()


def test_a_claim_type_with_attribution_is_accepted(db: Session, story: Story) -> None:
    entity = Entity(canonical_name="pytest Claimant", entity_type="PERSON")
    db.add(entity)
    db.flush()

    db.add(_claim(story, claim_type="CLAIM", attributed_to_entity_id=entity.id))
    db.flush()  # must not raise


# ------------------------------------------------------------------------ evidence


def test_claim_evidence_links_a_claim_to_its_source(
    db: Session, story: Story, article: RawArticle, source: Source
) -> None:
    claim = _claim(story)
    db.add(claim)
    db.flush()

    db.add(
        ClaimEvidence(
            claim_id=claim.id,
            raw_article_id=article.id,
            source_id=source.id,
            quote="the exact supporting sentence",
            url=article.canonical_url,
            stance="supports",
        )
    )
    db.flush()  # must not raise


def test_deleting_a_claim_cascades_to_its_evidence(
    db: Session, story: Story, article: RawArticle, source: Source
) -> None:
    claim = _claim(story)
    db.add(claim)
    db.flush()
    evidence = ClaimEvidence(
        claim_id=claim.id,
        raw_article_id=article.id,
        source_id=source.id,
        quote="pytest quote",
        url=article.canonical_url,
        stance="supports",
    )
    db.add(evidence)
    db.flush()
    evidence_id = evidence.id

    db.delete(claim)
    db.flush()
    db.expire_all()  # the DB cascade deleted the row; the identity map doesn't know yet

    assert db.get(ClaimEvidence, evidence_id) is None


def test_deleting_a_story_cascades_to_its_claims(db: Session, story: Story) -> None:
    claim = _claim(story)
    db.add(claim)
    db.flush()
    claim_id = claim.id

    db.delete(story)
    db.flush()
    db.expire_all()  # the DB cascade deleted the row; the identity map doesn't know yet

    assert db.get(Claim, claim_id) is None


# --------------------------------------------------------------------- prompt_versions


def test_a_second_active_version_of_the_same_name_is_rejected(db: Session) -> None:
    """DATABASE.md §9: exactly one active version per name. Two active rows for the
    same prompt would leave `ai_runs.prompt_version_id` pointing at an ambiguous
    'current' template."""
    db.add(
        PromptVersion(
            name="pytest_claim_extract",
            version=1,
            template="v1 template",
            checksum=hashlib.sha256(b"v1").digest(),
            is_active=True,
        )
    )
    db.flush()

    db.add(
        PromptVersion(
            name="pytest_claim_extract",
            version=2,
            template="v2 template",
            checksum=hashlib.sha256(b"v2").digest(),
            is_active=True,
        )
    )
    with pytest.raises(IntegrityError, match="ix_prompt_versions_one_active"):
        db.flush()


def test_an_inactive_second_version_is_accepted(db: Session) -> None:
    db.add(
        PromptVersion(
            name="pytest_claim_extract_2",
            version=1,
            template="v1 template",
            checksum=hashlib.sha256(b"v1").digest(),
            is_active=True,
        )
    )
    db.flush()

    db.add(
        PromptVersion(
            name="pytest_claim_extract_2",
            version=2,
            template="v2 template",
            checksum=hashlib.sha256(b"v2").digest(),
            is_active=False,
        )
    )
    db.flush()  # must not raise


def test_the_same_name_and_version_cannot_repeat(db: Session) -> None:
    db.add(
        PromptVersion(
            name="pytest_claim_extract_3",
            version=1,
            template="v1",
            checksum=hashlib.sha256(b"v1").digest(),
        )
    )
    db.flush()

    db.add(
        PromptVersion(
            name="pytest_claim_extract_3",
            version=1,
            template="v1 again",
            checksum=hashlib.sha256(b"v1-again").digest(),
            is_active=False,
        )
    )
    with pytest.raises(IntegrityError, match="uq_prompt_versions_name_version"):
        db.flush()


# --------------------------------------------------------------------------- ai_runs


def test_an_ai_run_records_a_model_call_against_a_story(db: Session, story: Story) -> None:
    """No prompt_version_id, job_id or article_id required -- a run must be loggable
    even before the rest of the pipeline around it exists, which is the whole reason
    this table was built before the extraction task that will call it."""
    run = AiRun(
        story_id=story.id,
        purpose="extract",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        input_tokens=1200,
        output_tokens=340,
        status="ok",
        request_digest=hashlib.sha256(b"pytest request").digest(),
    )
    db.add(run)
    db.flush()

    assert run.cost is None  # no model_pricing yet -- see the model docstring
    assert run.id is not None


def test_an_ai_run_can_name_the_prompt_version_that_produced_it(db: Session, story: Story) -> None:
    prompt = PromptVersion(
        name="pytest_claim_extract_4",
        version=1,
        template="extract claims from: {{articles}}",
        checksum=hashlib.sha256(b"template").digest(),
    )
    db.add(prompt)
    db.flush()

    run = AiRun(
        story_id=story.id,
        prompt_version_id=prompt.id,
        purpose="extract",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        status="ok",
    )
    db.add(run)
    db.flush()

    fetched = db.get(AiRun, run.id)
    assert fetched is not None
    assert fetched.prompt_version_id == prompt.id


def test_deleting_a_story_does_not_delete_its_ai_runs(db: Session, story: Story) -> None:
    """ai_runs.story_id is SET NULL on delete, not CASCADE -- the run happened and cost
    real money regardless of what became of the story afterward; the audit trail must
    survive the row it was about."""
    run = AiRun(
        story_id=story.id, purpose="extract", provider="anthropic", model="test", status="ok"
    )
    db.add(run)
    db.flush()
    run_id = run.id

    db.delete(story)
    db.flush()
    db.expire_all()  # the DB set story_id NULL directly; the identity map doesn't know yet

    fetched = db.get(AiRun, run_id)
    assert fetched is not None
    assert fetched.story_id is None
