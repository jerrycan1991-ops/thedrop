"""Reuniting a singleton straggler with a larger story (clustering.rejoin_stragglers).

Written against a finding: `label_recall.py --missed` surfaced several "same_event"
pairs blocked neither by threshold nor by the entity guard -- the two conditions
`consolidate_stories` checks -- yet still two separate stories. Tracing three real
examples showed all landed comfortably inside the 48h window, ruling out that
explanation too; the actual cause was a story-spanning article the digest rule
correctly declined to join, or two near-duplicate articles landing in the same
dispatch batch before either existed as a candidate for the other. Once split, only
`consolidate_stories` puts stories back together, and its threshold (0.90) is stricter
than the original join bar (0.82) -- so a pair scoring 0.82-0.90 could join fresh but
could never be reunited. `rejoin_stragglers` is the deliberately narrow fix: only a
singleton may be the candidate being reunited, and only into a strictly larger story,
at the original join threshold.

Needs a real Postgres. Every test rolls back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_database import engine
from thedrop_database.clustering import cluster_article, rejoin_stragglers
from thedrop_database.models import (
    Entity,
    Provider,
    RawArticle,
    RawArticleEntity,
    Source,
    Story,
)

pytestmark = pytest.mark.db

TEST_DOMAIN = "pytest-straggler-fixture.invalid"
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
        slug="pytest-straggler-provider",
        display_name="pytest",
        adapter_class="thedrop_ingest.providers.rss.RSSProvider",
        enabled=False,
        config={"feed_url": f"https://{TEST_DOMAIN}/feed.xml"},
    )
    db.add(created)
    db.flush()
    return created


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
        url_hash=(900_000 + n).to_bytes(32, "big"),
        title=f"Fixture {n}",
        published_at=FIXTURE_EPOCH + timedelta(hours=n),
        discovered_at=FIXTURE_EPOCH + timedelta(hours=n),
        injection_flags={"patterns": []},
        entities_extracted_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def link(db: Session, art: RawArticle, ent: Entity) -> None:
    db.add(RawArticleEntity(raw_article_id=art.id, entity_id=ent.id, mention_count=1))
    db.flush()


def vector(seed: int, tilt: float = 0.0) -> list[float]:
    v = [0.0] * 384
    v[seed % 384] = 1.0
    if tilt:
        v[(seed + 1) % 384] = tilt
    return v


def embedded_article(
    db: Session, provider: Provider, src: Source, n: int, vec: list[float]
) -> RawArticle:
    art = article_from(db, provider, src, n)
    art.embedding = vec
    art.embedded_at = datetime.now(UTC)
    db.flush()
    return art


def build_larger_story(
    db: Session,
    provider: Provider,
    src: Source,
    base_n: int,
    place: Entity,
    seed: int,
    tilt: float = 0.05,
    base_tilt: float = 0.0,
    join_threshold: float = 0.5,
) -> int:
    """Two articles that join live, producing a two-member (source_count==1 but
    2-article) story -- see the module docstring for why article COUNT, not
    source_count, is what makes something a rejoin candidate here.

    `base_tilt` shifts BOTH founding articles away from the raw `vector(seed)`. Needed
    when a test builds a SECOND large story sharing a seed with a first one: with
    base_tilt=0 article `a` is always identical between the two calls, so the second
    story's own construction can accidentally live-join the first rather than founding
    its own. `join_threshold` is exposed too, for the same reason -- a tight threshold
    lets the second story's two (now merely close, not identical) founding articles
    still join each other while staying below whatever similarity they have to the
    first story.
    """
    a = embedded_article(db, provider, src, base_n, vector(seed, base_tilt))
    b = embedded_article(db, provider, src, base_n + 1, vector(seed, base_tilt + tilt))
    link(db, a, place)
    link(db, b, place)
    first = cluster_article(db, a, join_threshold=join_threshold)
    second = cluster_article(db, b, join_threshold=join_threshold)
    assert second.joined, "fixture is wrong: the two founding articles should have joined"
    return first.story_id


def build_singleton(
    db: Session,
    provider: Provider,
    src: Source,
    n: int,
    place: Entity,
    seed: int,
    tilt: float = 0.0,
) -> int:
    art = embedded_article(db, provider, src, n, vector(seed, tilt))
    link(db, art, place)
    result = cluster_article(db, art, join_threshold=0.999)
    return result.story_id


def test_a_singleton_rejoins_a_larger_matching_story(db: Session, provider: Provider) -> None:
    src = named_source(db, "pytest-straggler.invalid")
    place = exact_entity(db, "pytest-straggler-place", "PLACE")
    larger = build_larger_story(db, provider, src, 100, place, seed=50)
    straggler = build_singleton(db, provider, src, 103, place, seed=50, tilt=0.08)
    assert straggler != larger, "fixture is wrong: these must start as separate stories"

    rejoins = rejoin_stragglers(db, join_threshold=0.5)

    pair = [r for r in rejoins if r.absorbed_id == straggler]
    assert pair, f"the straggler was not rejoined: {rejoins}"
    assert pair[0].survivor_id == larger


def test_the_larger_story_survives_not_the_older_one(db: Session, provider: Provider) -> None:
    """Opposite of consolidate_stories' rule on purpose: a singleton rejoining an
    established story is what a live join would have produced, and the straggler was
    founded first here (lower article numbers -> earlier published_at)."""
    src = named_source(db, "pytest-straggler2.invalid")
    place = exact_entity(db, "pytest-straggler-place2", "PLACE")
    # Built in this order (larger first) so the straggler's forced-singleton join
    # threshold (0.999, inside build_singleton) has something to actually resist --
    # article numbering, not construction order, is what makes it "founded first".
    larger = build_larger_story(db, provider, src, 111, place, seed=60, tilt=0.05)
    straggler = build_singleton(db, provider, src, 110, place, seed=60, tilt=0.3)

    rejoins = rejoin_stragglers(db, join_threshold=0.5)

    pair = [r for r in rejoins if r.absorbed_id == straggler]
    assert pair
    assert pair[0].survivor_id == larger, "the larger story should survive, not the older one"


def test_the_entity_guard_still_applies(db: Session, provider: Provider) -> None:
    src = named_source(db, "pytest-straggler3.invalid")
    place_a = exact_entity(db, "pytest-straggler-ohio", "PLACE")
    place_b = exact_entity(db, "pytest-straggler-nevada", "PLACE")
    build_larger_story(db, provider, src, 120, place_a, seed=70)
    straggler = build_singleton(db, provider, src, 123, place_b, seed=70, tilt=0.05)

    rejoins = rejoin_stragglers(db, join_threshold=0.5)

    assert not [r for r in rejoins if r.absorbed_id == straggler], (
        "two different events were rejoined on similarity alone"
    )
    story = db.get(Story, straggler)
    assert story is not None
    assert story.merged_into_id is None


def test_two_singletons_never_rejoin_each_other(db: Session, provider: Provider) -> None:
    """The deliberate scope limit: even an obvious duplicate pair stays apart here if
    neither side has ever grown past one article. consolidate_stories' job, at its own
    higher bar -- not widened into this pass."""
    src = named_source(db, "pytest-straggler4.invalid")
    place = exact_entity(db, "pytest-straggler-place4", "PLACE")
    first = build_singleton(db, provider, src, 130, place, seed=80)
    # tilt=0.1 -> cosine ~0.995, safely below the 0.999 threshold used to force
    # separation below, so the fixture actually produces two distinct stories.
    second = build_singleton(db, provider, src, 131, place, seed=80, tilt=0.1)
    assert first != second

    rejoins = rejoin_stragglers(db, join_threshold=0.5)

    assert rejoins == [], f"two singletons rejoined each other: {rejoins}"


def test_a_straggler_with_no_similar_story_is_left_alone(db: Session, provider: Provider) -> None:
    src = named_source(db, "pytest-straggler5.invalid")
    place = exact_entity(db, "pytest-straggler-place5", "PLACE")
    build_larger_story(db, provider, src, 140, place, seed=90)
    unrelated = build_singleton(db, provider, src, 143, place, seed=200)

    rejoins = rejoin_stragglers(db, join_threshold=0.82)

    assert not [r for r in rejoins if r.absorbed_id == unrelated]


def test_the_best_matching_larger_story_is_chosen(db: Session, provider: Provider) -> None:
    src = named_source(db, "pytest-straggler6.invalid")
    place = exact_entity(db, "pytest-straggler-place6", "PLACE")
    closer = build_larger_story(db, provider, src, 150, place, seed=100, tilt=0.05)
    # base_tilt=0.6 shifts "farther" well away from "closer" (whose members sit near
    # base_tilt=0), so its own founding article does not accidentally live-join
    # "closer" during construction -- see build_larger_story's docstring.
    farther = build_larger_story(
        db, provider, src, 160, place, seed=100, base_tilt=0.6, tilt=0.05, join_threshold=0.9
    )
    # tilt=0.15: close enough to "closer" (~0.99 similarity) to clearly outrank
    # "farther" (~0.92), but far enough that build_singleton's own 0.999 forced-
    # separation threshold does not accidentally live-join it to "closer" already.
    straggler = build_singleton(db, provider, src, 170, place, seed=100, tilt=0.15)

    rejoins = rejoin_stragglers(db, join_threshold=0.3)

    pair = [r for r in rejoins if r.absorbed_id == straggler]
    assert pair
    assert pair[0].survivor_id == closer, "chose a farther match over a closer one"
    assert pair[0].survivor_id != farther


def test_an_already_absorbed_straggler_is_not_considered_twice(
    db: Session, provider: Provider
) -> None:
    src = named_source(db, "pytest-straggler7.invalid")
    place = exact_entity(db, "pytest-straggler-place7", "PLACE")
    larger = build_larger_story(db, provider, src, 180, place, seed=110)
    # tilt=0.3, not the same 0.05 build_larger_story's second member uses: matching it
    # exactly put the straggler's own construction (join_threshold=0.999) close enough
    # to that specific member to accidentally join live, leaving nothing to rejoin.
    straggler = build_singleton(db, provider, src, 183, place, seed=110, tilt=0.3)

    first_pass = rejoin_stragglers(db, join_threshold=0.5)
    second_pass = rejoin_stragglers(db, join_threshold=0.5)

    assert [r for r in first_pass if r.absorbed_id == straggler]
    assert second_pass == [], "an already-absorbed straggler was reconsidered"
    story = db.get(Story, straggler)
    assert story is not None
    assert story.merged_into_id == larger


def test_a_pair_between_join_and_merge_threshold_can_still_rejoin(
    db: Session, provider: Provider
) -> None:
    """The entire point: this must succeed at join_threshold even though it would fail
    consolidate_stories' stricter merge_threshold."""
    src = named_source(db, "pytest-straggler8.invalid")
    place = exact_entity(db, "pytest-straggler-place8", "PLACE")
    larger = build_larger_story(db, provider, src, 190, place, seed=120, tilt=0.0)
    # cosine(vector(120), vector(120, 0.6)) = 1/sqrt(1+0.6^2) ~= 0.857 -- deliberately
    # between the two thresholds this test exercises (0.80 and 0.90), not pinned to
    # either exactly.
    straggler = build_singleton(db, provider, src, 193, place, seed=120, tilt=0.6)

    blocked = rejoin_stragglers(db, join_threshold=0.90)
    assert not [r for r in blocked if r.absorbed_id == straggler], (
        "fixture is wrong: this pair should NOT clear a 0.90 bar"
    )

    rejoined = rejoin_stragglers(db, join_threshold=0.80)
    pair = [r for r in rejoined if r.absorbed_id == straggler]
    assert pair, "a pair below merge_threshold but above join_threshold was not rejoined"
    assert pair[0].survivor_id == larger


def test_a_stale_straggler_outside_the_window_is_not_rejoined(
    db: Session, provider: Provider
) -> None:
    src = named_source(db, "pytest-straggler9.invalid")
    place = exact_entity(db, "pytest-straggler-place9", "PLACE")
    build_larger_story(db, provider, src, 200, place, seed=130)
    straggler = build_singleton(db, provider, src, 203, place, seed=130, tilt=0.05)

    story = db.get(Story, straggler)
    assert story is not None
    story.last_activity_at = datetime.now(UTC) - timedelta(hours=49)
    db.flush()

    rejoins = rejoin_stragglers(db, join_threshold=0.5, window_hours=48)

    assert not [r for r in rejoins if r.absorbed_id == straggler]
