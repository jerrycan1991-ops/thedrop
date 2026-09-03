"""US relevance scoring against a real story (PIPELINE.md §7).

These exercise the two implemented signals end to end -- entity matching and publisher
country -- against real rows, plus the source-country fix this scoring stage exposed:
`sources.country` defaulted to "US" for every source and nothing had ever corrected it,
including `theguardian.com`, which is genuinely UK-headquartered.

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
    Entity,
    Provider,
    RawArticle,
    Source,
    Story,
    StoryEntity,
    StorySource,
)
from thedrop_database.scoring import (
    score_us_relevance,
    unscored_story_ids,
    update_us_relevance,
)
from thedrop_ingest.pipeline import _NON_US_DOMAINS, resolve_source

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-scoring-fixture.invalid"
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
        slug="pytest-scoring-provider",
        display_name="pytest",
        adapter_class="thedrop_ingest.providers.rss.RSSProvider",
        enabled=False,
        config={"feed_url": f"https://{TEST_DOMAIN}/feed.xml"},
    )
    db.add(created)
    db.flush()
    return created


def source(db: Session, domain: str, country: str = "US") -> Source:
    row = Source(domain=domain, name=domain, country=country)
    db.add(row)
    db.flush()
    return row


def entity(db: Session, name: str, kind: str = "PLACE") -> Entity:
    row = Entity(canonical_name=name, entity_type=kind)
    db.add(row)
    db.flush()
    return row


def article(db: Session, provider: Provider, src: Source, n: int) -> RawArticle:
    url = f"https://{src.domain}/{n}"
    row = RawArticle(
        provider_id=provider.id,
        source_id=src.id,
        canonical_url=url,
        original_url=url,
        url_hash=(900_000 + n).to_bytes(32, "big"),
        title=f"Scoring fixture {n}",
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
    )
    db.add(row)
    db.flush()
    return row


def build_story(
    db: Session,
    provider: Provider,
    members: list[tuple[Source, list[Entity]]],
    *,
    n_start: int,
) -> Story:
    """A story with the given (source, entities) members, without going through
    clustering -- scoring.py must work on any story shape, not just ones join-or-create
    would actually produce."""
    story = Story(title="Scoring fixture story", first_seen_at=FIXTURE_EPOCH)
    db.add(story)
    db.flush()

    all_entities: set[int] = set()
    for i, (src, entities) in enumerate(members):
        art = article(db, provider, src, n_start + i)
        db.add(StorySource(story_id=story.id, raw_article_id=art.id, is_primary=(i == 0)))
        for ent in entities:
            all_entities.add(ent.id)
    db.flush()

    for entity_id in all_entities:
        db.add(StoryEntity(story_id=story.id, entity_id=entity_id, mention_count=1))
    db.flush()

    return story


# ------------------------------------------------------------------- entity signal


def test_a_story_naming_only_us_places_scores_full_marks_on_entities(
    db: Session, provider: Provider
) -> None:
    texas = entity(db, "Texas")
    fbi = entity(db, "FBI", kind="ORG")
    src = source(db, "pytest-scoring-us.invalid")

    story = build_story(db, provider, [(src, [texas, fbi])], n_start=1)
    result = score_us_relevance(db, story.id)

    assert result.entity_signal == pytest.approx(1.0)
    assert set(result.matched_entities) == {"Texas", "FBI"}


def test_a_story_naming_only_foreign_places_scores_zero_on_entities(
    db: Session, provider: Provider
) -> None:
    nepal = entity(db, "Nepal")
    tibet = entity(db, "Tibet")
    src = source(db, "pytest-scoring-foreign.invalid")

    story = build_story(db, provider, [(src, [nepal, tibet])], n_start=10)
    result = score_us_relevance(db, story.id)

    assert result.entity_signal == 0.0
    assert result.matched_entities == []


def test_other_typed_entities_do_not_count_either_way(db: Session, provider: Provider) -> None:
    """The same noise the clustering guard excludes must not move a score either --
    consistency between the two matters, not just correctness in isolation."""
    noise = entity(db, "American", kind="OTHER")
    src = source(db, "pytest-scoring-noise.invalid")

    story = build_story(db, provider, [(src, [noise])], n_start=20)
    result = score_us_relevance(db, story.id)

    # Only OTHER-typed entities on the story: excluded from the denominator too, or
    # this would divide zero by zero.
    assert result.entity_signal == 0.0


# ---------------------------------------------------------------- publisher signal


def test_all_us_publishers_scores_full_marks_on_publisher_share(
    db: Session, provider: Provider
) -> None:
    a = source(db, "pytest-scoring-pub-a.invalid", country="US")
    b = source(db, "pytest-scoring-pub-b.invalid", country="US")
    placeholder = entity(db, "pytest-scoring-anchor")

    story = build_story(db, provider, [(a, [placeholder]), (b, [])], n_start=30)
    result = score_us_relevance(db, story.id)

    assert result.publisher_signal == pytest.approx(1.0)
    assert result.us_sources == 2
    assert result.total_sources == 2


def test_a_mixed_publisher_story_scores_the_true_fraction(db: Session, provider: Provider) -> None:
    us_source = source(db, "pytest-scoring-mix-us.invalid", country="US")
    gb_source = source(db, "pytest-scoring-mix-gb.invalid", country="GB")
    placeholder = entity(db, "pytest-scoring-anchor2")

    story = build_story(db, provider, [(us_source, [placeholder]), (gb_source, [])], n_start=40)
    result = score_us_relevance(db, story.id)

    assert result.publisher_signal == pytest.approx(0.5)


# ------------------------------------------------------------------------ rescaling


def test_full_marks_on_both_signals_stores_a_score_of_100(db: Session, provider: Provider) -> None:
    """The end-to-end check that rescaling actually reaches 100, not the 50-point cap
    a naive weighted sum of two of five signals would produce."""
    texas = entity(db, "Texas")
    src = source(db, "pytest-scoring-full.invalid", country="US")

    story = build_story(db, provider, [(src, [texas])], n_start=50)
    result = update_us_relevance(db, story.id)

    assert result.score == 100
    db.refresh(story)
    assert story.us_relevance_score == 100
    assert story.scores_computed_at is not None
    assert story.us_relevance_basis["coverage"] == 0.50


def test_zero_on_both_signals_stores_a_score_of_zero(db: Session, provider: Provider) -> None:
    nepal = entity(db, "Nepal")
    src = source(db, "pytest-scoring-zero.invalid", country="GB")

    story = build_story(db, provider, [(src, [nepal])], n_start=60)
    result = update_us_relevance(db, story.id)

    assert result.score == 0


def test_entities_and_publisher_share_are_weighted_60_40(db: Session, provider: Provider) -> None:
    """0.30 and 0.20 rescaled over their 0.50 combined weight are 0.60 and 0.40 -- a
    story with full marks on entities alone must score noticeably higher than one with
    full marks on publisher share alone, in that specific ratio."""
    # Entity-only: a NON-US source (so publisher_signal=0) naming a US place.
    texas = entity(db, "Texas")
    gb_src = source(db, "pytest-scoring-entonly.invalid", country="GB")
    entity_heavy = build_story(db, provider, [(gb_src, [texas])], n_start=70)

    # Publisher-only: a US source (so publisher_signal=1), naming a foreign place
    # (so entity_signal=0).
    nepal = entity(db, "Nepal")
    us_src = source(db, "pytest-scoring-pubonly.invalid", country="US")
    publisher_heavy = build_story(db, provider, [(us_src, [nepal])], n_start=80)

    entity_result = score_us_relevance(db, entity_heavy.id)
    publisher_result = score_us_relevance(db, publisher_heavy.id)

    assert entity_result.score == 60
    assert publisher_result.score == 40


# --------------------------------------------------------------------- dispatch


def test_unscored_story_ids_finds_stories_with_no_score(db: Session, provider: Provider) -> None:
    texas = entity(db, "Texas")
    src = source(db, "pytest-scoring-dispatch.invalid")
    story = build_story(db, provider, [(src, [texas])], n_start=90)

    assert story.id in unscored_story_ids(db, limit=1000)

    update_us_relevance(db, story.id)
    db.flush()

    assert story.id not in unscored_story_ids(db, limit=1000)


def test_a_merged_story_is_never_offered_for_scoring(db: Session, provider: Provider) -> None:
    """A story that was absorbed by consolidation has nothing left to score -- its
    articles moved to the survivor, which will be scored (or rescored) instead."""
    texas = entity(db, "Texas")
    src = source(db, "pytest-scoring-merged.invalid")
    survivor = build_story(db, provider, [(src, [texas])], n_start=100)
    absorbed = build_story(db, provider, [(src, [texas])], n_start=101)
    absorbed.merged_into_id = survivor.id
    db.flush()

    assert absorbed.id not in unscored_story_ids(db, limit=1000)


# --------------------------------------------------------- the source.country fix


def test_a_known_non_us_domain_is_classified_correctly_on_creation(
    db: Session,
) -> None:
    """Reproduces the finding: `resolve_source` used to create every new source under
    the blind "US" default. `theguardian.com` sat misclassified for as long as this
    override did not exist -- feeding a silently wrong value into publisher_signal."""
    assert "theguardian.com" in _NON_US_DOMAINS

    created = resolve_source(db, "https://theguardian.com/us-news/some-story")
    assert created.country == "GB"


def test_an_unlisted_domain_still_gets_the_us_default(db: Session) -> None:
    """The override must not become a requirement -- an outlet that genuinely is US,
    and simply is not yet on the list, should keep working exactly as before."""
    created = resolve_source(db, f"https://{TEST_DOMAIN}/some-story")
    assert created.country == "US"
