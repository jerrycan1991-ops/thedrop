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
    cluster_article,
    consolidate_stories,
    guard_entity_ids,
    overexposed_entity_ids,
    overexposure_threshold,
    shared_guard_entities,
    story_guard_entities,
)
from thedrop_database.models import (
    Entity,
    Provider,
    RawArticle,
    RawArticleEntity,
    Source,
    Story,
    StoryEntity,
    StorySource,
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


# ------------------------------------------------------------- join or create

# Vectors are built by hand so similarity is exact rather than whatever the model
# happens to produce. Two articles about the same event have near-identical embeddings;
# the guard is what has to separate two near-identical articles about DIFFERENT events,
# which is precisely the case a similarity threshold cannot see.


def vector(seed: int, tilt: float = 0.0) -> list[float]:
    """A unit-ish vector. `tilt` moves it away from the base direction."""
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


def test_two_near_identical_articles_about_different_events_do_not_merge(
    db: Session, provider: Provider
) -> None:
    """The whole reason the guard exists.

    Both articles are 99.5% similar — the wording of two shootings is nearly the same —
    so a similarity threshold alone merges them. Only the place names separate them.
    """
    src = named_source(db, "pytest-wire.invalid")
    ohio = embedded_article(db, provider, src, 100, vector(5, 0.1))
    nevada = embedded_article(db, provider, src, 101, vector(5, 0.1))
    link(db, ohio, exact_entity(db, "pytest-dayton", "PLACE"))
    link(db, nevada, exact_entity(db, "pytest-reno", "PLACE"))

    first = cluster_article(db, ohio, join_threshold=0.5)
    second = cluster_article(db, nevada, join_threshold=0.5)

    assert not second.joined, "two different events were merged into one story"
    assert first.story_id != second.story_id


def test_two_accounts_of_the_same_event_do_merge(db: Session, provider: Provider) -> None:
    """The guard must not be so strict that nothing clusters."""
    src = named_source(db, "pytest-wire2.invalid")
    shared_entity = exact_entity(db, "pytest-dayton2", "PLACE")
    first_article = embedded_article(db, provider, src, 110, vector(7))
    second_article = embedded_article(db, provider, src, 111, vector(7, 0.05))
    link(db, first_article, shared_entity)
    link(db, second_article, shared_entity)

    founded = cluster_article(db, first_article, join_threshold=0.5)
    joined = cluster_article(db, second_article, join_threshold=0.5)

    assert joined.joined
    assert joined.story_id == founded.story_id
    assert joined.similarity is not None
    assert joined.similarity > 0.5
    assert joined.shared_entities == 1


def test_a_shared_entity_alone_does_not_merge_dissimilar_articles(
    db: Session, provider: Provider
) -> None:
    """Both conditions are required. Two unrelated stories about the same senator share
    an entity and must still stay apart."""
    src = named_source(db, "pytest-wire3.invalid")
    senator = exact_entity(db, "pytest-senator", "PERSON")
    budget = embedded_article(db, provider, src, 120, vector(11))
    scandal = embedded_article(db, provider, src, 121, vector(200))
    link(db, budget, senator)
    link(db, scandal, senator)

    founded = cluster_article(db, budget, join_threshold=0.82)
    other = cluster_article(db, scandal, join_threshold=0.82)

    assert not other.joined
    assert other.story_id != founded.story_id


def test_membership_and_centroid_are_recorded(db: Session, provider: Provider) -> None:
    """A join must be auditable: the similarity that justified it is stored, because the
    centroid moves afterwards and the number cannot be recomputed."""
    src = named_source(db, "pytest-wire4.invalid")
    shared_entity = exact_entity(db, "pytest-place4", "PLACE")
    a = embedded_article(db, provider, src, 130, vector(13))
    b = embedded_article(db, provider, src, 131, vector(13, 0.05))
    link(db, a, shared_entity)
    link(db, b, shared_entity)

    founded = cluster_article(db, a, join_threshold=0.5)
    cluster_article(db, b, join_threshold=0.5)

    members = db.scalars(select(StorySource).where(StorySource.story_id == founded.story_id)).all()
    assert len(members) == 2
    assert any(m.is_primary for m in members)
    assert any(m.similarity is not None for m in members)

    story = db.get(Story, founded.story_id)
    assert story is not None
    assert story.centroid is not None
    assert story.source_count == 1, "both articles came from one source"


def test_independent_source_count_is_never_inferred(db: Session, provider: Provider) -> None:
    """ADR-0013: nothing may infer independence from source identity. Forty outlets
    carrying one wire story are forty sources and one witness, so a corroboration rule
    downstream must not find a number here that was guessed."""
    src = named_source(db, "pytest-wire5.invalid")
    art = embedded_article(db, provider, src, 140, vector(17))
    link(db, art, exact_entity(db, "pytest-place5", "PLACE"))

    decision = cluster_article(db, art)
    story = db.get(Story, decision.story_id)

    assert story is not None
    assert story.independent_source_count == 0


def test_an_article_without_entities_founds_its_own_story(db: Session, provider: Provider) -> None:
    """No discriminative entity means nothing says which event it belongs to. Founding a
    story is the correct answer, not an error."""
    src = named_source(db, "pytest-wire6.invalid")
    a = embedded_article(db, provider, src, 150, vector(19))
    b = embedded_article(db, provider, src, 151, vector(19))
    link(db, a, exact_entity(db, "pytest-place6", "PLACE"))

    founded = cluster_article(db, a, join_threshold=0.5)
    orphan = cluster_article(db, b, join_threshold=0.5)

    assert not orphan.joined
    assert orphan.story_id != founded.story_id


# ------------------------------------------------------------------- digests


def test_an_article_spanning_unrelated_stories_joins_neither(
    db: Session, provider: Provider
) -> None:
    """The NPR "Up First" case, from the first real clustering run.

    A morning digest covers several unrelated events, so it is similar enough to each
    and shares a discriminative entity with each — and it passed the guard against the
    ICE story while the shutdown it also covered sat in a separate story.

    Letting it join is worse than it sounds: a digest is not a second SOURCE for a
    story, it is a paragraph in a summary, and a verification step counting members
    would read it as corroboration.
    """
    src = named_source(db, "pytest-digest.invalid")
    ice = exact_entity(db, "pytest-ice", "ORG")
    congress = exact_entity(db, "pytest-congress", "ORG")

    # Two unrelated stories: orthogonal vectors, different entities.
    ice_article = embedded_article(db, provider, src, 200, vector(21))
    shutdown_article = embedded_article(db, provider, src, 201, vector(300))
    link(db, ice_article, ice)
    link(db, shutdown_article, congress)
    first = cluster_article(db, ice_article, join_threshold=0.5)
    second = cluster_article(db, shutdown_article, join_threshold=0.5)
    assert first.story_id != second.story_id, "fixture is wrong: these should be separate"

    # The digest sits between them and mentions both.
    digest = embedded_article(db, provider, src, 202, vector(21, 1.0))
    digest.embedding = [(a + b) / 2 for a, b in zip(vector(21), vector(300), strict=True)]
    db.flush()
    link(db, digest, ice)
    link(db, digest, congress)

    decision = cluster_article(db, digest, join_threshold=0.5)

    assert not decision.joined
    assert decision.story_id not in (first.story_id, second.story_id)
    assert "digest" in decision.reason


def test_matching_two_stories_about_the_same_event_still_joins(
    db: Session, provider: Provider
) -> None:
    """Two similar stories are not a digest — they are one event that clustering has
    under-split, and the article belongs to the better of them.

    Without this distinction the digest rule would block exactly the joins that repair
    over-splitting, which is the failure mode the whole design leans towards.
    """
    src = named_source(db, "pytest-dup.invalid")
    shared_entity = exact_entity(db, "pytest-leipzig", "PLACE")

    first_article = embedded_article(db, provider, src, 210, vector(23))
    # 0.1 rather than 0.02: at 0.02 the cosine is 0.9998 and they merge even at a
    # 0.999 threshold, so the fixture would not have set up the case it claims to.
    second_article = embedded_article(db, provider, src, 211, vector(23, 0.1))
    link(db, first_article, shared_entity)
    link(db, second_article, shared_entity)

    a = cluster_article(db, first_article, join_threshold=0.999)
    b = cluster_article(db, second_article, join_threshold=0.999)
    assert a.story_id != b.story_id, "fixture is wrong: these should be two stories"

    # A third article about the same event, now with a threshold both can clear.
    third = embedded_article(db, provider, src, 212, vector(23, 0.05))
    link(db, third, shared_entity)

    decision = cluster_article(db, third, join_threshold=0.5)

    assert decision.joined, f"refused a legitimate join: {decision.reason}"
    assert decision.story_id in (a.story_id, b.story_id)


# -------------------------------------------------------------- consolidation


def test_two_stories_about_one_event_are_merged(db: Session, provider: Provider) -> None:
    """The counterweight to a design that leans towards over-splitting.

    Join-or-create refuses whenever it is unsure and the digest rule refuses again;
    both produce duplicate stories for one event. Nothing else puts them back together.
    """
    src = named_source(db, "pytest-cons.invalid")
    place = exact_entity(db, "pytest-leipzig2", "PLACE")
    a = embedded_article(db, provider, src, 300, vector(31))
    b = embedded_article(db, provider, src, 301, vector(31, 0.1))
    link(db, a, place)
    link(db, b, place)

    first = cluster_article(db, a, join_threshold=0.999)
    second = cluster_article(db, b, join_threshold=0.999)
    assert first.story_id != second.story_id, "fixture is wrong: these should be two stories"

    merges = consolidate_stories(db, merge_threshold=0.9)

    pair = [
        m for m in merges if {m.survivor_id, m.absorbed_id} == {first.story_id, second.story_id}
    ]
    assert pair, f"the duplicate stories were not merged: {merges}"
    assert pair[0].survivor_id == first.story_id, "the older story should survive"


def test_a_merge_moves_the_articles_and_records_where_they_went(
    db: Session, provider: Provider
) -> None:
    """A deleted row cannot explain where its articles went. PIPELINE.md requires the
    merge to stay auditable, so the absorbed story is kept and points at its survivor."""
    src = named_source(db, "pytest-cons2.invalid")
    place = exact_entity(db, "pytest-place-cons", "PLACE")
    a = embedded_article(db, provider, src, 310, vector(37))
    b = embedded_article(db, provider, src, 311, vector(37, 0.1))
    link(db, a, place)
    link(db, b, place)
    first = cluster_article(db, a, join_threshold=0.999)
    second = cluster_article(db, b, join_threshold=0.999)

    consolidate_stories(db, merge_threshold=0.9)

    members = db.scalars(
        select(StorySource.raw_article_id).where(StorySource.story_id == first.story_id)
    ).all()
    assert set(members) == {a.id, b.id}

    absorbed = db.get(Story, second.story_id)
    assert absorbed is not None, "the absorbed story was deleted; the merge is unauditable"
    assert absorbed.merged_into_id == first.story_id

    db.refresh(b)
    assert b.story_id == first.story_id


def test_consolidation_does_not_bypass_the_entity_guard(db: Session, provider: Provider) -> None:
    """The important one.

    If a merge needed only similarity, consolidation would be a way around the guard --
    two stories the guard kept apart at join time would be reunited a minute later, and
    every US story would eventually collapse into one.
    """
    src = named_source(db, "pytest-cons3.invalid")
    a = embedded_article(db, provider, src, 320, vector(41))
    b = embedded_article(db, provider, src, 321, vector(41, 0.05))
    link(db, a, exact_entity(db, "pytest-ohio-c", "PLACE"))
    link(db, b, exact_entity(db, "pytest-nevada-c", "PLACE"))

    first = cluster_article(db, a, join_threshold=0.999)
    second = cluster_article(db, b, join_threshold=0.999)

    merges = consolidate_stories(db, merge_threshold=0.5)

    assert not [
        m for m in merges if {m.survivor_id, m.absorbed_id} == {first.story_id, second.story_id}
    ], "two different events were merged on similarity alone"


def test_a_merged_story_is_not_considered_again(db: Session, provider: Provider) -> None:
    """An absorbed story keeps its row. If consolidation still saw it as a candidate it
    would merge an empty shell into something else on every pass."""
    src = named_source(db, "pytest-cons4.invalid")
    place = exact_entity(db, "pytest-place-c4", "PLACE")
    a = embedded_article(db, provider, src, 330, vector(43))
    b = embedded_article(db, provider, src, 331, vector(43, 0.1))
    link(db, a, place)
    link(db, b, place)
    cluster_article(db, a, join_threshold=0.999)
    cluster_article(db, b, join_threshold=0.999)

    first_pass = consolidate_stories(db, merge_threshold=0.9)
    second_pass = consolidate_stories(db, merge_threshold=0.9)

    assert first_pass
    assert second_pass == [], "consolidation repeated itself on an already-merged story"


# --------------------------------------------------------------- alias exposure


def test_a_common_entity_cannot_slip_through_under_a_longer_name(
    db: Session, provider: Provider
) -> None:
    """The leak, reproduced.

    At 393 articles the ceiling was 40. "Trump" appeared in 97 and was correctly
    excluded; "Donald Trump" appeared in 18 and was admitted. The same person was both
    blocked and allowed depending on which surface form an article used, so the ceiling
    leaked under an alias — and that costs precision, which is the one thing the guard
    exists to protect.
    """
    src = named_source(db, "pytest-alias.invalid")
    short = exact_entity(db, "pytest-trump", "PERSON")
    long_form = exact_entity(db, "pytest-donald pytest-trump", "PERSON")

    # The short form is everywhere; the long form is rare.
    for n in range(400, 408):
        link(db, article_from(db, provider, src, n), short)
    rare = article_from(db, provider, src, 420)
    link(db, rare, long_form)

    excluded = overexposed_entity_ids(db, max_fraction=0.001, min_floor=4)

    assert short.id in excluded, "fixture is wrong: the short form should be over-exposed"
    assert long_form.id in excluded, "the long form slipped through under an alias"
    assert guard_entity_ids(db, rare.id, max_fraction=0.001, min_floor=4) == set()


def test_grouping_needs_a_whole_word_suffix(db: Session, provider: Provider) -> None:
    """ "Ian" must not be absorbed by "Iran". Substring matching would group unrelated
    entities and quietly exclude discriminative ones."""
    src = named_source(db, "pytest-alias2.invalid")
    country = exact_entity(db, "pytest-iran", "PLACE")
    person = exact_entity(db, "pytest-ian", "PLACE")

    for n in range(430, 438):
        link(db, article_from(db, provider, src, n), country)
    rare = article_from(db, provider, src, 440)
    link(db, rare, person)

    excluded = overexposed_entity_ids(db, max_fraction=0.001, min_floor=4)

    assert country.id in excluded
    assert person.id not in excluded, "a substring match absorbed an unrelated entity"


def test_two_people_sharing_a_surname_are_not_grouped(db: Session, provider: Provider) -> None:
    """The rule fires only when the corpus contains BOTH forms. Nothing named
    "pytest-smith" exists here, so these two stay independent."""
    src = named_source(db, "pytest-alias3.invalid")
    john = exact_entity(db, "pytest-john pytest-smith", "PERSON")
    jane = exact_entity(db, "pytest-jane pytest-smith", "PERSON")

    for n in range(450, 458):
        link(db, article_from(db, provider, src, n), john)
    rare = article_from(db, provider, src, 460)
    link(db, rare, jane)

    excluded = overexposed_entity_ids(db, max_fraction=0.001, min_floor=4)

    assert john.id in excluded
    assert jane.id not in excluded, "unrelated people were grouped by a shared surname"


# -------------------------------------------------------------- masthead leak


def test_a_singletons_own_masthead_cannot_license_a_join(db: Session, provider: Provider) -> None:
    """The gap found reading real output: story 26, founded by one npr.org article,
    had "NPR" itself sitting unfiltered in its own guard set.

    `guard_entity_ids` excludes an entity that is the CANDIDATE article's own masthead.
    Nothing excluded it on the STORY side -- so a second article from a different
    outlet merely mentioning "NPR reported..." could join a one-article NPR story on
    the strength of NPR naming itself, not on anything the two articles actually share.
    """
    npr = named_source(db, "pytest-mast-npr.invalid")
    other = named_source(db, "pytest-mast-other.invalid")
    masthead = exact_entity(db, "pytest-mast-npr")

    founder = embedded_article(db, provider, npr, 500, vector(60))
    link(db, founder, masthead)
    cluster_article(db, founder, join_threshold=0.999)

    incoming = embedded_article(db, provider, other, 501, vector(60, 0.05))
    link(db, incoming, masthead)

    joined = cluster_article(db, incoming, join_threshold=0.5)

    assert not joined.joined, "joined a singleton on the strength of its own masthead"


def test_the_filter_lifts_once_a_second_outlet_joins(db: Session, provider: Provider) -> None:
    """A story is not "single-publisher" forever. Once an axios.com article has
    genuinely joined an npr.org story, "NPR" is no longer self-attribution for every
    member -- a THIRD article citing NPR as its subject is a real signal and must not
    be thrown away by a filter that no longer applies.
    """
    npr = named_source(db, "pytest-mast2-npr.invalid")
    axios = named_source(db, "pytest-mast2-axios.invalid")
    masthead = exact_entity(db, "pytest-mast2-npr")
    place = exact_entity(db, "pytest-mast2-place")

    founder = embedded_article(db, provider, npr, 510, vector(65))
    link(db, founder, masthead)
    link(db, founder, place)
    founder_result = cluster_article(db, founder, join_threshold=0.999)

    second = embedded_article(db, provider, axios, 511, vector(65, 0.02))
    link(db, second, masthead)
    link(db, second, place)
    joined_second = cluster_article(db, second, join_threshold=0.5)
    assert joined_second.joined, "fixture is wrong: the second article should join on 'place'"

    guarded = story_guard_entities(db, founder_result.story_id)
    assert masthead.id in guarded, "the filter did not lift once a second outlet joined"


def test_a_genuine_cross_outlet_reference_still_counts(db: Session, provider: Provider) -> None:
    """The filter must not overreach. An outlet naming ANOTHER outlet as its subject
    is real signal, not self-attribution, and must still license a join."""
    axios = named_source(db, "pytest-mast3-axios.invalid")
    other = named_source(db, "pytest-mast3-other.invalid")
    npr_as_subject = exact_entity(db, "pytest-mast3-npr")

    founder = embedded_article(db, provider, axios, 520, vector(70))
    link(db, founder, npr_as_subject)
    cluster_article(db, founder, join_threshold=0.999)

    incoming = embedded_article(db, provider, other, 521, vector(70, 0.05))
    link(db, incoming, npr_as_subject)
    joined = cluster_article(db, incoming, join_threshold=0.5)

    assert joined.joined, "excluded a genuine subject reference, not self-attribution"
