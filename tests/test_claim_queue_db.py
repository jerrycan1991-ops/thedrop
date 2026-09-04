"""Dispatching claim-extraction jobs (thedrop_database.claim_queue), PIPELINE.md §10-11.

Same shape as tests/test_entity_extraction_db.py's queueing section, plus one thing
that dispatcher does not need to check: a story still inside its clustering join
window must not be dispatched, because extraction has no automatic re-trigger when a
story later gains a member -- see claim_queue.unclaimed_story_ids's docstring.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.claim_queue import (
    JOB_TYPE,
    MAX_TEXT_CHARS_PER_ARTICLE,
    enqueue_extraction_batches,
    unclaimed_story_ids,
)
from thedrop_database.enums import JobStatus
from thedrop_database.models import Job, Provider, RawArticle, Source, Story, StorySource

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-claim-queue-fixture.invalid"
FIXTURE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
WINDOW_HOURS = 48


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
        slug="pytest-claim-queue-provider",
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
    created = Source(domain=TEST_DOMAIN, name="pytest claim queue fixture", country="US")
    db.add(created)
    db.flush()
    return created


def _now() -> datetime:
    return datetime.now(UTC)


# The dispatch cutoff is computed against the real wall clock (claim_queue.py:
# `datetime.now(UTC) - timedelta(hours=window_hours)`), not against FIXTURE_EPOCH --
# so these have to be anchored to real "now" too, or both would land equally far in
# the past relative to the actual cutoff and the distinction this file exists to test
# would silently stop meaning anything.
def _stale() -> datetime:
    return _now() - timedelta(hours=WINDOW_HOURS + 1)  # past the join window


def _fresh() -> datetime:
    return _now()  # still open to new members


def add_story(
    db: Session,
    provider: Provider,
    source: Source,
    *,
    n: int,
    last_activity_at: datetime | None = None,
    title: str | None = None,
    body: str | None = "Police in Dayton, Ohio said the suspect acted alone.",
    merged: bool = False,
) -> Story:
    last_activity_at = last_activity_at if last_activity_at is not None else _stale()
    url = f"https://{TEST_DOMAIN}/{n}"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(800_000 + n).to_bytes(32, "big"),
        title=title if title is not None else f"pytest claim queue fixture {n}",
        body_text=body,
        published_at=FIXTURE_EPOCH,
        discovered_at=FIXTURE_EPOCH,
        injection_flags={"patterns": []},
    )
    db.add(article)
    db.flush()

    story = Story(
        title=f"pytest claim queue fixture story {n}",
        first_seen_at=FIXTURE_EPOCH,
        last_activity_at=last_activity_at,
    )
    db.add(story)
    db.flush()
    db.add(StorySource(story_id=story.id, raw_article_id=article.id, is_primary=True))
    article.story_id = story.id
    if merged:
        # Any other story id works; the point is merged_into_id is not null.
        story.merged_into_id = story.id
    db.flush()
    return story


# --------------------------------------------------------------- unclaimed_story_ids


def test_a_story_past_its_window_is_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=1, last_activity_at=_stale())
    assert story.id in unclaimed_story_ids(db, window_hours=WINDOW_HOURS, limit=100)


def test_a_story_still_inside_its_window_is_not_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    """The whole reason this gate exists: extracting from a story that is still
    accumulating members bakes an incomplete evidence packet in permanently, since
    nothing re-triggers extraction when a later article joins."""
    story = add_story(db, provider, source, n=2, last_activity_at=_fresh())
    assert story.id not in unclaimed_story_ids(db, window_hours=WINDOW_HOURS, limit=100)


def test_a_merged_story_is_never_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=3, last_activity_at=_stale(), merged=True)
    assert story.id not in unclaimed_story_ids(db, window_hours=WINDOW_HOURS, limit=100)


def test_an_already_extracted_story_is_not_a_candidate(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=4, last_activity_at=_stale())
    story.claims_extracted_at = datetime.now(UTC)
    db.flush()
    assert story.id not in unclaimed_story_ids(db, window_hours=WINDOW_HOURS, limit=100)


# --------------------------------------------------------------------- queueing


def test_only_eligible_stories_are_queued(db: Session, provider: Provider, source: Source) -> None:
    waiting = add_story(db, provider, source, n=10, last_activity_at=_stale())
    still_open = add_story(db, provider, source, n=11, last_activity_at=_fresh())

    enqueue_extraction_batches(db, window_hours=WINDOW_HOURS, max_batches=1)

    queued = {
        item["id"]
        for job in db.scalars(select(Job).where(Job.job_type == JOB_TYPE)).all()
        for item in job.payload["items"]
    }
    assert str(waiting.public_id) in queued
    assert str(still_open.public_id) not in queued


def test_the_payload_carries_articles_nested_under_the_story(
    db: Session, provider: Provider, source: Source
) -> None:
    story = add_story(db, provider, source, n=20, last_activity_at=_stale())

    enqueue_extraction_batches(db, window_hours=WINDOW_HOURS, max_batches=1)

    job = db.scalar(select(Job).where(Job.job_type == JOB_TYPE))
    assert job is not None
    item = next(i for i in job.payload["items"] if i["id"] == str(story.public_id))
    assert len(item["articles"]) == 1
    assert item["articles"][0]["source"] == source.domain
    assert "Dayton" in item["articles"][0]["text"]


def test_the_article_text_is_bounded(db: Session, provider: Provider, source: Source) -> None:
    add_story(
        db,
        provider,
        source,
        n=30,
        last_activity_at=_stale(),
        body="x" * (MAX_TEXT_CHARS_PER_ARTICLE * 2),
    )

    enqueue_extraction_batches(db, window_hours=WINDOW_HOURS, max_batches=1)

    job = db.scalar(select(Job).where(Job.job_type == JOB_TYPE))
    assert job is not None
    text = job.payload["items"][0]["articles"][0]["text"]
    assert len(text) == MAX_TEXT_CHARS_PER_ARTICLE


def test_a_story_with_no_extractable_text_is_skipped(
    db: Session, provider: Provider, source: Source
) -> None:
    # title alone (no dek, no body_text) is enough text for a real article; genuinely
    # empty text needs the title blanked too -- title is NOT NULL but not
    # minimum-length-checked, so an empty string is a real, if unusual, possible value.
    add_story(db, provider, source, n=40, last_activity_at=_stale(), title="", body=None)

    result = enqueue_extraction_batches(db, window_hours=WINDOW_HOURS, max_batches=1)

    assert result == []
    assert db.scalar(select(Job).where(Job.job_type == JOB_TYPE)) is None


def test_a_tick_while_work_is_outstanding_queues_nothing_new(
    db: Session, provider: Provider, source: Source
) -> None:
    for n in range(50, 54):
        add_story(db, provider, source, n=n, last_activity_at=_stale())

    first = enqueue_extraction_batches(
        db, window_hours=WINDOW_HOURS, stories_per_batch=2, max_batches=4
    )
    second = enqueue_extraction_batches(
        db, window_hours=WINDOW_HOURS, stories_per_batch=2, max_batches=4
    )

    assert first, "the first dispatch queued nothing, so this proves nothing"
    assert second == []


def test_clearing_the_marker_re_queues_finished_work(
    db: Session, provider: Provider, source: Source
) -> None:
    """Same regression test as entity extraction's: idempotency_key must not be a
    function of the batch's contents, or a completed job blocks a legitimate
    re-dispatch forever."""
    for n in range(70, 74):
        add_story(db, provider, source, n=n, last_activity_at=_stale())

    first = enqueue_extraction_batches(
        db, window_hours=WINDOW_HOURS, stories_per_batch=4, max_batches=1
    )
    assert first

    db.execute(update(Job).where(Job.job_type == JOB_TYPE).values(status=JobStatus.DONE))
    db.execute(update(Story).values(claims_extracted_at=datetime.now(UTC)))
    db.flush()
    assert (
        enqueue_extraction_batches(
            db, window_hours=WINDOW_HOURS, stories_per_batch=4, max_batches=1
        )
        == []
    )

    db.execute(update(Story).values(claims_extracted_at=None))
    db.flush()

    assert enqueue_extraction_batches(
        db, window_hours=WINDOW_HOURS, stories_per_batch=4, max_batches=1
    ), "clearing the marker did not re-queue; backfills are impossible again"
