"""Which shared entities may license a cluster join.

Written against a finding rather than a hypothesis. On the first real corpus of 152
articles the top entity was "United States", in 28 of them — 18%. PIPELINE.md §6's rule
as literally written ("≥ 1 shared salient entity") passes for any two of those, leaving
cosine similarity to decide alone, which is the exact situation the guard exists to
prevent: a US tariff story and a US shooting both mention the United States.

So these pin the stricter rule — an entity licenses a join only when it discriminates —
and, just as importantly, that the rule does not go so far that nothing ever clusters.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.clustering import (
    guard_entity_ids,
    overexposed_entity_ids,
    overexposure_threshold,
    shared_guard_entities,
)
from thedrop_database.models import (
    Entity,
    Provider,
    RawArticle,
    RawArticleEntity,
    Source,
    Story,
    StoryEntity,
)

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-guard-fixture.invalid"
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
        slug="pytest-guard-provider",
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
    created = Source(domain=TEST_DOMAIN, name="pytest guard fixture")
    db.add(created)
    db.flush()
    return created


def article(db: Session, provider: Provider, source: Source, n: int) -> RawArticle:
    url = f"https://{TEST_DOMAIN}/{n}"
    row = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(700_000 + n).to_bytes(32, "big"),
        title=f"Fixture {n}",
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
        entities_extracted_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def entity(db: Session, name: str, kind: str = "PLACE") -> Entity:
    row = Entity(canonical_name=f"{name} [pytest]", entity_type=kind)
    db.add(row)
    db.flush()
    return row


def link(db: Session, art: RawArticle, ent: Entity) -> None:
    db.add(RawArticleEntity(raw_article_id=art.id, entity_id=ent.id, mention_count=1))
    db.flush()


# ------------------------------------------------------------------- threshold


def test_the_threshold_never_falls_below_the_floor(db: Session) -> None:
    """A young corpus must not exclude everything. At 20 articles a bare 10% ceiling
    would reject any entity seen twice, and nothing would ever cluster."""
    assert overexposure_threshold(db, max_fraction=0.10, min_floor=5) >= 5


def test_the_threshold_scales_with_the_corpus(db: Session) -> None:
    tiny = overexposure_threshold(db, max_fraction=0.0001, min_floor=1)
    broad = overexposure_threshold(db, max_fraction=1.0, min_floor=1)
    assert broad >= tiny


# ------------------------------------------------------------------ exposure


def test_an_entity_in_too_many_articles_cannot_license_a_join(
    db: Session, provider: Provider, source: Source
) -> None:
    """The finding, reproduced. "United States" in 18% of the corpus must not be what
    makes a tariff story and a shooting into one story."""
    everywhere = entity(db, "United States")
    articles = [article(db, provider, source, n) for n in range(1, 8)]
    for art in articles:
        link(db, art, everywhere)

    assert everywhere.id in overexposed_entity_ids(db, max_fraction=0.001, min_floor=3)
    assert guard_entity_ids(db, articles[0].id, max_fraction=0.001, min_floor=3) == set()


def test_a_rare_entity_still_licenses_a_join(
    db: Session, provider: Provider, source: Source
) -> None:
    """The guard must not be so strict that nothing clusters. "Dayton" in two articles
    is precisely the signal it is supposed to act on."""
    rare = entity(db, "Dayton")
    first = article(db, provider, source, 20)
    second = article(db, provider, source, 21)
    link(db, first, rare)
    link(db, second, rare)

    assert rare.id in guard_entity_ids(db, first.id, max_fraction=0.10, min_floor=5)


# ---------------------------------------------------------------------- type


def test_other_typed_entities_are_stored_but_never_license_a_join(
    db: Session, provider: Provider, source: Source
) -> None:
    """MISC is where this model's noise lands -- "American", "Rep". Real observations,
    worth keeping, but they say nothing about which event an article is about."""
    noise = entity(db, "American", kind="OTHER")
    art = article(db, provider, source, 30)
    link(db, art, noise)

    stored = db.scalars(
        select(RawArticleEntity.entity_id).where(RawArticleEntity.raw_article_id == art.id)
    ).all()
    assert noise.id in stored, "it should still be recorded"
    assert noise.id not in guard_entity_ids(db, art.id, max_fraction=0.10, min_floor=5)


# -------------------------------------------------------------------- sharing


def test_a_story_and_an_article_sharing_a_rare_entity_pass_the_guard(
    db: Session, provider: Provider, source: Source
) -> None:
    rare = entity(db, "Dayton")
    art = article(db, provider, source, 40)
    link(db, art, rare)

    story = Story(title="Dayton shooting")
    db.add(story)
    db.flush()
    db.add(StoryEntity(story_id=story.id, entity_id=rare.id, mention_count=1))
    db.flush()

    assert shared_guard_entities(db, art.id, story.id, max_fraction=0.10, min_floor=5) == {rare.id}


def test_sharing_only_a_common_entity_fails_the_guard(
    db: Session, provider: Provider, source: Source
) -> None:
    """Two articles that have nothing in common but the country they happened in."""
    everywhere = entity(db, "United States")
    articles = [article(db, provider, source, n) for n in range(50, 56)]
    for art in articles:
        link(db, art, everywhere)

    story = Story(title="Something else entirely in the United States")
    db.add(story)
    db.flush()
    db.add(StoryEntity(story_id=story.id, entity_id=everywhere.id, mention_count=1))
    db.flush()

    shared = shared_guard_entities(db, articles[0].id, story.id, max_fraction=0.001, min_floor=3)
    assert shared == set()


def test_an_article_with_no_discriminative_entities_joins_nothing(
    db: Session, provider: Provider, source: Source
) -> None:
    """The correct outcome, not a bug: nothing about it says which event it belongs to,
    so it becomes its own story and waits for consolidation or a human."""
    art = article(db, provider, source, 60)
    link(db, art, entity(db, "American", kind="OTHER"))

    story = Story(title="Anything")
    db.add(story)
    db.flush()

    assert guard_entity_ids(db, art.id) == set()
    assert shared_guard_entities(db, art.id, story.id) == set()


# ------------------------------------------------------------------ publisher


def named_source(db: Session, domain: str) -> Source:
    row = Source(domain=domain, name=domain)
    db.add(row)
    db.flush()
    return row


def exact_entity(db: Session, name: str, kind: str = "ORG") -> Entity:
    row = Entity(canonical_name=name, entity_type=kind)
    db.add(row)
    db.flush()
    return row


def article_from(db: Session, provider: Provider, src: Source, n: int) -> RawArticle:
    url = f"https://{src.domain}/{n}"
    row = RawArticle(
        provider_id=provider.id,
        source_id=src.id,
        canonical_url=url,
        original_url=url,
        url_hash=(800_000 + n).to_bytes(32, "big"),
        title=f"Fixture {n}",
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
        entities_extracted_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def test_an_article_cannot_be_clustered_by_its_own_masthead(
    db: Session, provider: Provider
) -> None:
    """ "NPR" in an NPR article is attribution, not evidence about the event.

    Found in real data: NPR appeared in 8 of 152 articles, below the discriminative
    ceiling, so two unrelated NPR stories could have passed the guard on the strength of
    sharing their publisher. Every outlet that names itself in its own copy does this.
    """
    npr = named_source(db, "pytest-npr.invalid")
    masthead = exact_entity(db, "pytest-npr")
    art = article_from(db, provider, npr, 1)
    link(db, art, masthead)

    assert masthead.id not in guard_entity_ids(db, art.id, max_fraction=0.10, min_floor=5)


def test_the_same_name_still_counts_in_someone_else_s_article(
    db: Session, provider: Provider
) -> None:
    """The filter is about attribution, not about the word. A piece in another outlet
    ABOUT NPR has NPR as a genuine subject, and must keep it."""
    npr = named_source(db, "pytest-npr2.invalid")
    other = named_source(db, "pytest-elsewhere.invalid")
    subject = exact_entity(db, "pytest-npr2")

    own = article_from(db, provider, npr, 10)
    theirs = article_from(db, provider, other, 11)
    link(db, own, subject)
    link(db, theirs, subject)

    assert subject.id not in guard_entity_ids(db, own.id, max_fraction=0.10, min_floor=5)
    assert subject.id in guard_entity_ids(db, theirs.id, max_fraction=0.10, min_floor=5)


def test_a_subdomain_publisher_is_matched_on_every_label(db: Session, provider: Provider) -> None:
    """`science.nasa.gov` is NASA. Matching only the first label would let a NASA press
    release cluster with an unrelated NASA press release on the word NASA."""
    src = named_source(db, "pytest-sci.pytest-nasa.invalid")
    masthead = exact_entity(db, "pytest-nasa")
    art = article_from(db, provider, src, 20)
    link(db, art, masthead)

    assert masthead.id not in guard_entity_ids(db, art.id, max_fraction=0.10, min_floor=5)
