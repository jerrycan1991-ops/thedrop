"""Dispatching contradiction-check jobs (thedrop_database.contradiction_queue),
PIPELINE.md §11.

Same shape as tests/test_claim_queue_db.py's queueing section, minus the window-hours
gate that dispatcher needs and this one does not -- see
contradiction_queue.uncontested_story_ids's docstring for why: a contradiction check
reads claims that already exist rather than betting on a story being done
accumulating members. What is unique to this dispatcher: a story needs at least two
claims to be worth a model call at all.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.contradiction_queue import (
    JOB_TYPE,
    enqueue_contradiction_batches,
    uncontested_story_ids,
)
from thedrop_database.enums import JobStatus
from thedrop_database.models import (
    Claim,
    Entity,
    Job,
    Provider,
    RawArticle,
    Source,
    Story,
    StorySource,
)

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-contradiction-queue-fixture.invalid"
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
        slug="pytest-contradiction-queue-provider",
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
    created = Source(domain=TEST_DOMAIN, name="pytest contradiction queue fixture", country="US")
    db.add(created)
    db.flush()
    return created


def add_story(
    db: Session,
    provider: Provider,
    source: Source,
    *,
    n: int,
    num_claims: int = 2,
    claims_extracted: bool = True,
    contradictions_checked: bool = False,
    merged: bool = False,
    attributed_to: Entity | None = None,
) -> Story:
    url = f"https://{TEST_DOMAIN}/{n}"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(900_000 + n).to_bytes(32, "big"),
        title=f"pytest contradiction queue fixture {n}",
        published_at=FIXTURE_EPOCH,
        discovered_at=FIXTURE_EPOCH,
        injection_flags={"patterns": []},
    )
    db.add(article)
    db.flush()

    story = Story(
        title=f"pytest contradiction queue fixture story {n}",
        first_seen_at=FIXTURE_EPOCH,
        claims_extracted_at=FIXTURE_EPOCH if claims_extracted else None,
        contradictions_checked_at=datetime.now(UTC) if contradictions_checked else None,
    )
    db.add(story)
    db.flush()
    db.add(StorySource(story_id=story.id, raw_article_id=article.id, is_primary=True))
    article.story_id = story.id
    if merged:
        story.merged_into_id = story.id

    for i in range(num_claims):
        claim = Claim(
            story_id=story.id,
            claim_text=f"pytest contradiction queue claim {n}-{i}",
            claim_type="FACT",
            confidence=80,
            attributed_to_entity_id=attributed_to.id if attributed_to else None,
        )
        db.add(claim)
    db.flush()
    return story


# ------------------------------------------------------------------ uncontested_story_ids


def test_a_story_with_extracted_claims_is_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=1)
    assert story.id in uncontested_story_ids(db, limit=100)


def test_a_story_without_extracted_claims_is_not_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=2, claims_extracted=False)
    assert story.id not in uncontested_story_ids(db, limit=100)


def test_a_story_with_only_one_claim_is_not_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    """Not worth a model call -- find_contradictions itself short-circuits on fewer
    than two checkable claims, and this dispatcher knows that in advance."""
    story = add_story(db, provider, source, n=3, num_claims=1)
    assert story.id not in uncontested_story_ids(db, limit=100)


def test_an_already_checked_story_is_not_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=4, contradictions_checked=True)
    assert story.id not in uncontested_story_ids(db, limit=100)


def test_a_merged_story_is_never_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=5, merged=True)
    assert story.id not in uncontested_story_ids(db, limit=100)


# --------------------------------------------------------------------------- queueing


def test_only_eligible_stories_are_queued(db: Session, provider: Provider, source: Source) -> None:
    waiting = add_story(db, provider, source, n=10)
    not_extracted = add_story(db, provider, source, n=11, claims_extracted=False)

    enqueue_contradiction_batches(db, max_batches=1)

    queued = {
        item["id"]
        for job in db.scalars(select(Job).where(Job.job_type == JOB_TYPE)).all()
        for item in job.payload["items"]
    }
    assert str(waiting.public_id) in queued
    assert str(not_extracted.public_id) not in queued


def test_the_payload_carries_claims_nested_under_the_story(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=20)

    enqueue_contradiction_batches(db, max_batches=1)

    job = db.scalar(select(Job).where(Job.job_type == JOB_TYPE))
    assert job is not None
    item = next(i for i in job.payload["items"] if i["id"] == str(story.public_id))
    assert len(item["claims"]) == 2
    assert all(c["type"] == "FACT" for c in item["claims"])
    assert all(c["attributedTo"] == "" for c in item["claims"])


def test_attribution_name_is_carried_when_present(
    db: Session, provider: Provider, source: Source
) -> None:
    entity = Entity(canonical_name="Mayor Elena Ruiz", entity_type="OTHER")
    db.add(entity)
    db.flush()
    story = add_story(db, provider, source, n=30, attributed_to=entity)

    enqueue_contradiction_batches(db, max_batches=1)

    job = db.scalar(select(Job).where(Job.job_type == JOB_TYPE))
    assert job is not None
    item = next(i for i in job.payload["items"] if i["id"] == str(story.public_id))
    assert all(c["attributedTo"] == "Mayor Elena Ruiz" for c in item["claims"])


def test_a_tick_while_work_is_outstanding_queues_nothing_new(
    db: Session, provider: Provider, source: Source
) -> None:
    for n in range(50, 54):
        add_story(db, provider, source, n=n)

    first = enqueue_contradiction_batches(db, stories_per_batch=2, max_batches=4)
    second = enqueue_contradiction_batches(db, stories_per_batch=2, max_batches=4)

    assert first, "the first dispatch queued nothing, so this proves nothing"
    assert second == []


def test_clearing_the_marker_re_queues_finished_work(
    db: Session, provider: Provider, source: Source
) -> None:
    """Same regression test as claim extraction's: idempotency_key must not be a
    function of the batch's contents, or a completed job blocks a legitimate
    re-dispatch forever."""
    for n in range(70, 74):
        add_story(db, provider, source, n=n)

    first = enqueue_contradiction_batches(db, stories_per_batch=4, max_batches=1)
    assert first

    db.execute(update(Job).where(Job.job_type == JOB_TYPE).values(status=JobStatus.DONE))
    db.execute(update(Story).values(contradictions_checked_at=datetime.now(UTC)))
    db.flush()
    assert enqueue_contradiction_batches(db, stories_per_batch=4, max_batches=1) == []

    db.execute(update(Story).values(contradictions_checked_at=None))
    db.flush()

    assert enqueue_contradiction_batches(
        db, stories_per_batch=4, max_batches=1
    ), "clearing the marker did not re-queue; backfills are impossible again"
