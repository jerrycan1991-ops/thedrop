"""Turning un-embedded articles into work orders (Phase 3, ADR-0005).

Needs a real Postgres: the whole point of these is what the *database* does — the
unique constraint on `jobs.idempotency_key` collapsing a re-dispatched backlog, and
`embedding IS NULL` selecting against a pgvector column. Neither is observable in
Python.

Every test rolls back. Nothing here commits, so it is safe against the live database —
which is the only database available under ADR-0012.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

# `services/api` is already on sys.path — tests/conftest.py puts it there.
from app.routers.worker import EmbeddingItem, EmbeddingsRequest, store_embeddings
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_config import get_settings
from thedrop_database import engine
from thedrop_database.embedding_queue import (
    JOB_TYPE,
    MAX_TEXT_CHARS,
    enqueue_embedding_batches,
    pending_embedding_count,
)
from thedrop_database.models import Job, Provider, RawArticle, Source

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-embed-fixture.invalid"

#: Older than any real ingested article, so fixture rows always sort to the front of
#: the oldest-first dispatch query regardless of what the live database holds.
FIXTURE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def db() -> Iterator[Session]:
    """Always rolled back — see tests/test_ingest_pipeline_db.py."""
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
def source(db: Session) -> Source:
    created = Source(domain=TEST_DOMAIN, name="pytest embed fixture")
    db.add(created)
    db.flush()
    return created


@pytest.fixture
def provider(db: Session) -> Provider:
    existing = db.scalar(select(Provider).limit(1))
    if existing is not None:
        return existing
    created = Provider(
        slug="pytest-embed-provider",
        display_name="pytest",
        adapter_class="thedrop_ingest.providers.rss.RSSProvider",
        enabled=False,
        config={"feed_url": f"https://{TEST_DOMAIN}/feed.xml"},
    )
    db.add(created)
    db.flush()
    return created


def add_article(
    db: Session,
    provider: Provider,
    source: Source,
    *,
    n: int,
    title: str = "Senate passes budget bill",
    dek: str | None = "Approved 51-49 on Tuesday.",
    body: str | None = None,
    embedded: bool = False,
) -> RawArticle:
    """One raw article, ordered by `n`.

    `discovered_at` is pinned to a fixed date in the past, NOT `now - n hours`. These
    run against the live database (ADR-0012), which already holds real un-embedded
    articles; dispatch selects oldest-first, so a fixture row dated relative to now
    would land in or out of the batch depending on how much real backlog exists that
    day. Several of these tests would then pass without asserting anything.
    """
    url = f"https://{TEST_DOMAIN}/{n}"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=n.to_bytes(32, "big"),
        title=title,
        dek=dek,
        body_text=body,
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
        embedding=[0.0] * 384 if embedded else None,
    )
    db.add(article)
    db.flush()
    return article


def queued_jobs(db: Session) -> list[Job]:
    return list(db.scalars(select(Job).where(Job.job_type == JOB_TYPE)).all())


# ------------------------------------------------------------------- selection


def test_only_un_embedded_articles_are_queued(
    db: Session, provider: Provider, source: Source
) -> None:
    waiting = add_article(db, provider, source, n=1, embedded=False)
    done = add_article(db, provider, source, n=2, embedded=True)

    before = pending_embedding_count(db)
    enqueue_embedding_batches(db, batch_size=50, max_batches=1)

    ids = [item["id"] for job in queued_jobs(db) for item in job.payload["items"]]
    assert str(waiting.public_id) in ids
    assert str(done.public_id) not in ids, "an already-embedded article was re-queued"
    assert before >= 1
    assert len(ids) == len(set(ids)), "an article was queued twice in one dispatch"


def test_an_article_with_no_text_is_left_alone_not_consumed(
    db: Session, provider: Provider, source: Source
) -> None:
    """A title-less row is an ingestion defect. Queueing it would embed nothing;
    marking it done would hide it. It is skipped so it stays visible as pending."""
    empty = add_article(db, provider, source, n=91, title="", dek=None, body=None)
    neighbour = add_article(db, provider, source, n=92)

    enqueue_embedding_batches(db, batch_size=50, max_batches=1)

    queued_ids = {item["id"] for job in queued_jobs(db) for item in job.payload["items"]}
    # The neighbour proves selection actually reached these rows, so the absence of
    # `empty` is a decision rather than an artefact of it never being looked at.
    assert str(neighbour.public_id) in queued_ids
    assert str(empty.public_id) not in queued_ids
    assert empty.embedding is None


def test_the_payload_carries_title_and_dek_truncated(
    db: Session, provider: Provider, source: Source
) -> None:
    """bge-small truncates at 512 tokens, so anything beyond is paid for in payload
    size and thrown away by the tokeniser. Cut on the VPS, where the bound is a
    decision a handler cannot widen."""
    article = add_article(
        db, provider, source, n=92, title="Fed holds rates", dek="x" * (MAX_TEXT_CHARS * 2)
    )

    enqueue_embedding_batches(db, batch_size=50, max_batches=1)

    texts = {item["id"]: item["text"] for job in queued_jobs(db) for item in job.payload["items"]}
    text = texts[str(article.public_id)]
    assert text.startswith("Fed holds rates")
    assert len(text) == MAX_TEXT_CHARS


# --------------------------------------------------------------------- bounds


def test_a_tick_is_bounded_by_batch_size_and_max_batches(
    db: Session, provider: Provider, source: Source
) -> None:
    """A cold start must not put thousands of rows in front of a desktop that may be
    offline, starving everything queued behind them."""
    for n in range(100, 112):
        add_article(db, provider, source, n=n)

    enqueue_embedding_batches(db, batch_size=2, max_batches=3)

    jobs = queued_jobs(db)
    assert len(jobs) <= 3
    assert all(len(job.payload["items"]) <= 2 for job in jobs)


# ---------------------------------------------------------------- idempotency


def test_re_dispatching_an_unchanged_backlog_queues_nothing_new(
    db: Session, provider: Provider, source: Source
) -> None:
    """The property that makes a 120-second beat safe.

    The job key is derived from the batch's article ids, and `jobs.idempotency_key` is
    unique — so a second tick over the same backlog hits ON CONFLICT DO NOTHING instead
    of queueing the same work twice. Without this, every tick would pile up another
    copy of the whole backlog.
    """
    for n in range(200, 204):
        add_article(db, provider, source, n=n)

    first = enqueue_embedding_batches(db, batch_size=2, max_batches=4)
    count_after_first = len(queued_jobs(db))
    second = enqueue_embedding_batches(db, batch_size=2, max_batches=4)

    assert first, "the first dispatch queued nothing, so this proves nothing"
    assert second == []
    assert len(queued_jobs(db)) == count_after_first


# ----------------------------------------------------------------- storage API

# The endpoint is called directly rather than over HTTP: what is under test is the
# validation and the write, not FastAPI's routing. `db.commit()` inside it is contained
# by the fixture's outer transaction, so nothing reaches the live database.

# The REAL Settings object, not a stand-in. A hand-written fake with
# `embedding_model` directly on it is what let `settings.embedding_model` ship: the
# field actually lives on the nested `settings.ai`, so every one of these passed while
# the endpoint raised AttributeError in production. A fake that is easier to satisfy
# than the real thing tests the fake.
MODEL = get_settings().ai.embedding_model


@dataclass
class FakeNode:
    id: int = 1
    name: str = "desktop-test"


def unit(seed: int = 1) -> list[float]:
    vector = [0.0] * 384
    vector[seed % 384] = 1.0
    return vector


def test_a_batch_from_the_wrong_model_is_refused(db: Session) -> None:
    """ADR-0005 keeps ONE vector space. Mixing models corrupts similarity search in a
    way nothing errors on -- results just quietly get worse."""
    request = EmbeddingsRequest(
        model="intfloat/e5-base", items=[EmbeddingItem(id="a", vector=unit())]
    )

    with pytest.raises(HTTPException) as caught:
        store_embeddings(request, FakeNode(), db, get_settings())

    assert caught.value.status_code == 400
    assert "model mismatch" in caught.value.detail


def test_the_wrong_number_of_dimensions_is_refused(db: Session) -> None:
    request = EmbeddingsRequest(model=MODEL, items=[EmbeddingItem(id="a", vector=[0.1] * 768)])

    with pytest.raises(HTTPException) as caught:
        store_embeddings(request, FakeNode(), db, get_settings())

    assert caught.value.status_code == 400
    assert "768" in caught.value.detail


def test_an_unnormalized_vector_is_refused(db: Session) -> None:
    """bge returns unit vectors. Anything off the unit sphere came from a different
    model or a different pooling strategy, whatever it calls itself."""
    request = EmbeddingsRequest(model=MODEL, items=[EmbeddingItem(id="a", vector=[0.5] * 384)])

    with pytest.raises(HTTPException) as caught:
        store_embeddings(request, FakeNode(), db, get_settings())

    assert caught.value.status_code == 400
    assert "not normalized" in caught.value.detail


def test_nothing_is_written_when_any_item_in_the_batch_is_invalid(
    db: Session, provider: Provider, source: Source
) -> None:
    """A partially applied batch would leave the caller unable to say what happened
    without re-reading every row."""
    article = add_article(db, provider, source, n=300)
    request = EmbeddingsRequest(
        model=MODEL,
        items=[
            EmbeddingItem(id=str(article.public_id), vector=unit()),
            EmbeddingItem(id="b", vector=[0.1] * 768),
        ],
    )

    with pytest.raises(HTTPException):
        store_embeddings(request, FakeNode(), db, get_settings())

    db.refresh(article)
    assert article.embedding is None


def test_a_valid_batch_is_stored_and_timestamped(
    db: Session, provider: Provider, source: Source
) -> None:
    article = add_article(db, provider, source, n=301)

    result = store_embeddings(
        EmbeddingsRequest(
            model=MODEL, items=[EmbeddingItem(id=str(article.public_id), vector=unit(7))]
        ),
        FakeNode(),
        db,
        get_settings(),
    )

    assert result == {"stored": 1, "unknown": []}
    db.refresh(article)
    assert article.embedding is not None
    assert article.embedded_at is not None


def test_an_unknown_article_is_reported_not_an_error(db: Session) -> None:
    """A row can legitimately vanish between dispatch and completion. Reported so a
    runner that is systematically wrong is visible rather than silently writing
    nothing."""
    unknown_id = "00000000-0000-4000-8000-000000000000"

    result = store_embeddings(
        EmbeddingsRequest(model=MODEL, items=[EmbeddingItem(id=unknown_id, vector=unit())]),
        FakeNode(),
        db,
        get_settings(),
    )

    assert result == {"stored": 0, "unknown": [unknown_id]}


def test_the_settings_the_dispatcher_reads_actually_exist() -> None:
    """Stands in for `app.tasks.embed.dispatch_embedding_batches`, which cannot be
    imported here: services/api and services/worker both ship a top-level `app`, and
    conftest puts services/api on sys.path first.

    It exists because the task shipped reading `settings.embedding_batch_size` when the
    field lives on the nested `settings.ai`. Nothing caught it — no test called the
    task, and the endpoint tests injected a fake Settings that had the fields directly
    on it. It crash-looped every 120 seconds in production while the deploy reported
    six green gates.

    The expressions below are exactly the ones the dispatcher and the endpoint use.
    """
    ai = get_settings().ai

    assert isinstance(ai.embedding_batch_size, int)
    assert isinstance(ai.embedding_max_batches_per_tick, int)
    assert isinstance(ai.embedding_dimensions, int)
    assert isinstance(ai.embedding_model, str)
    assert ai.embedding_model
