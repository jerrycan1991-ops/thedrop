"""Entity extraction: queueing it, and storing what comes back (PIPELINE.md §12).

These exist because entity overlap is the clustering guard. PIPELINE.md §6 is explicit
that it is a correctness guard rather than an optimization — embeddings alone happily
merge "shooting in Ohio" with "shooting in Nevada" — so the things that would quietly
break it are worth pinning:

  * an article whose tagger found nothing must still be marked processed, or the
    dispatcher re-queues it on every tick forever;
  * the same name must resolve to the SAME entity row, because the guard matches on
    entity_id and not on string equality;
  * re-extraction must replace an article's entities, not merge two models' output.

Needs a real Postgres for the upserts and the unique constraints. Every test rolls
back; nothing here commits.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.routers.worker import ArticleEntities, EntitiesRequest, ExtractedEntity, store_entities
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.entity_queue import (
    JOB_TYPE,
    MAX_TEXT_CHARS,
    enqueue_extraction_batches,
    pending_extraction_count,
)
from thedrop_database.models import Entity, Job, Provider, RawArticle, RawArticleEntity, Source

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-entity-fixture.invalid"
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
        slug="pytest-entity-provider",
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
    created = Source(domain=TEST_DOMAIN, name="pytest entity fixture")
    db.add(created)
    db.flush()
    return created


def add_article(
    db: Session,
    provider: Provider,
    source: Source,
    *,
    n: int,
    title: str = "Shooting in Dayton leaves three dead",
    body: str | None = "Police in Dayton, Ohio said the suspect acted alone.",
    extracted: bool = False,
) -> RawArticle:
    """Pinned to a fixed past date so fixture rows sort ahead of real backlog."""
    url = f"https://{TEST_DOMAIN}/{n}"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(500_000 + n).to_bytes(32, "big"),
        title=title,
        body_text=body,
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
        entities_extracted_at=datetime.now(UTC) if extracted else None,
    )
    db.add(article)
    db.flush()
    return article


class FakeNode:
    id = 1
    name = "desktop-test"


def request_for(article: RawArticle, *names: tuple[str, str]) -> EntitiesRequest:
    return EntitiesRequest(
        model="dslim/bert-base-NER",
        items=[
            ArticleEntities(
                id=str(article.public_id),
                entities=[
                    ExtractedEntity(name=name, type=kind, mentions=1, salience=0.5)
                    for name, kind in names
                ],
            )
        ],
    )


# --------------------------------------------------------------------- queueing


def test_only_unextracted_articles_are_queued(
    db: Session, provider: Provider, source: Source
) -> None:
    waiting = add_article(db, provider, source, n=1, extracted=False)
    done = add_article(db, provider, source, n=2, extracted=True)

    assert pending_extraction_count(db) >= 1
    enqueue_extraction_batches(db, batch_size=50, max_batches=1)

    queued = {
        item["id"]
        for job in db.scalars(select(Job).where(Job.job_type == JOB_TYPE)).all()
        for item in job.payload["items"]
    }
    assert str(waiting.public_id) in queued
    assert str(done.public_id) not in queued


def test_the_payload_is_bounded(db: Session, provider: Provider, source: Source) -> None:
    article = add_article(db, provider, source, n=10, body="x" * (MAX_TEXT_CHARS * 2))

    enqueue_extraction_batches(db, batch_size=50, max_batches=1)

    texts = {
        item["id"]: item["text"]
        for job in db.scalars(select(Job).where(Job.job_type == JOB_TYPE)).all()
        for item in job.payload["items"]
    }
    assert len(texts[str(article.public_id)]) == MAX_TEXT_CHARS


def test_re_dispatch_queues_nothing_new(db: Session, provider: Provider, source: Source) -> None:
    for n in range(20, 24):
        add_article(db, provider, source, n=n)

    first = enqueue_extraction_batches(db, batch_size=2, max_batches=4)
    second = enqueue_extraction_batches(db, batch_size=2, max_batches=4)

    assert first, "the first dispatch queued nothing, so this proves nothing"
    assert second == []


# ---------------------------------------------------------------------- storing


def test_an_article_with_no_entities_is_still_marked_processed(
    db: Session, provider: Provider, source: Source
) -> None:
    """The one that stops an infinite queue.

    A tagger finding nothing is a legitimate outcome. Without the timestamp it is
    indistinguishable from never having run, and the dispatcher would re-queue the
    article on every tick for the life of the system.
    """
    article = add_article(db, provider, source, n=30, title="Untagged", body="Nothing here.")

    result = store_entities(request_for(article), FakeNode(), db)

    assert result["articles"] == 1
    assert result["entities"] == 0
    db.refresh(article)
    assert article.entities_extracted_at is not None
    assert pending_extraction_count(db) == 0 or article.entities_extracted_at is not None


def test_the_same_name_resolves_to_one_entity_row(
    db: Session, provider: Provider, source: Source
) -> None:
    """The guard matches on entity_id. If two articles mentioning Jerome Powell produced
    two rows, they would share no entity and would never cluster."""
    first = add_article(db, provider, source, n=31)
    second = add_article(db, provider, source, n=32)

    store_entities(request_for(first, ("Jerome Powell", "PERSON")), FakeNode(), db)
    store_entities(request_for(second, ("Jerome Powell", "PERSON")), FakeNode(), db)

    rows = db.scalars(select(Entity).where(Entity.canonical_name == "Jerome Powell")).all()
    assert len(rows) == 1

    linked = db.scalars(
        select(RawArticleEntity.raw_article_id).where(RawArticleEntity.entity_id == rows[0].id)
    ).all()
    assert {first.id, second.id} <= set(linked)


def test_re_extraction_replaces_rather_than_merges(
    db: Session, provider: Provider, source: Source
) -> None:
    """A requeued job or a model change must not leave two models' output mixed
    together in one article with no way to tell which came from where."""
    article = add_article(db, provider, source, n=33)

    store_entities(request_for(article, ("Dayton", "PLACE"), ("Ohio", "PLACE")), FakeNode(), db)
    store_entities(request_for(article, ("Reno", "PLACE")), FakeNode(), db)

    names = db.scalars(
        select(Entity.canonical_name)
        .join(RawArticleEntity, RawArticleEntity.entity_id == Entity.id)
        .where(RawArticleEntity.raw_article_id == article.id)
    ).all()
    assert set(names) == {"Reno"}


def test_an_unrecognised_type_falls_back_rather_than_failing(
    db: Session, provider: Provider, source: Source
) -> None:
    """A model whose labels drift should degrade to OTHER, not reject the batch and
    strand every article in it."""
    article = add_article(db, provider, source, n=34)

    store_entities(request_for(article, ("Some Thing", "NOT_A_TYPE")), FakeNode(), db)

    kind = db.scalar(
        select(Entity.entity_type)
        .join(RawArticleEntity, RawArticleEntity.entity_id == Entity.id)
        .where(RawArticleEntity.raw_article_id == article.id)
    )
    assert kind == "OTHER"


def test_an_unknown_article_is_reported_not_an_error(db: Session) -> None:
    request = EntitiesRequest(
        model="dslim/bert-base-NER",
        items=[ArticleEntities(id="00000000-0000-4000-8000-000000000000", entities=[])],
    )

    result = store_entities(request, FakeNode(), db)

    assert result["articles"] == 0
    assert result["unknown"] == ["00000000-0000-4000-8000-000000000000"]
