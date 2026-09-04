"""Which shared entities are allowed to license a cluster join.

PIPELINE.md §6 requires a shared salient entity before two articles may join, because
embeddings alone happily merge "shooting in Ohio" with "shooting in Nevada". Run against
the first real corpus, the rule as literally written turned out to be too weak:

    28  PLACE    United States     <- 18% of 152 articles
    19  PERSON   Trump
    10  PLACE    Iran
    10  ORG      NASA
     9  OTHER    American

Any two of those 28 articles share an entity, so the guard passes and cosine similarity
decides alone -- which is exactly the situation the guard exists to prevent. A US tariff
story and a US shooting both mention the United States. That fact carries almost no
information; "Dayton" carries a great deal.

So a shared entity licenses a join only when it is DISCRIMINATIVE. Two filters:

  * **type** -- OTHER is excluded. It is where this model's MISC label lands, and MISC
    is where the noise lives: "American", "Rep". Those are still stored, they just may
    not license a merge.
  * **document frequency** -- an entity appearing in more than a small share of the
    corpus is excluded. Standard IDF reasoning: a term in 18% of documents separates
    almost nothing.
  * **the article's own publisher** -- "NPR" in an NPR article is attribution, not
    evidence about the event. Every outlet that names itself in its own copy produces
    one of these, and two unrelated NPR stories sharing "NPR" would otherwise pass the
    guard. Excluded only for the article that publisher wrote: "NPR" in a Reuters piece
    ABOUT NPR is a genuine subject and still counts.

This makes the guard STRICTER than PIPELINE.md specifies, not looser. It can only cause
under-clustering, which is the safe direction (ADR-0015): duplicate stories are visible
and mergeable, a wrongly merged story is neither.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from thedrop_database.enums import EntityType
from thedrop_database.models import (
    Entity,
    RawArticle,
    RawArticleEntity,
    Source,
    Story,
    StoryEntity,
    StorySource,
)

logger = logging.getLogger(__name__)

#: MISC lands here, and MISC is nationalities, adjectives and truncated titles. Kept in
#: the database -- they are real observations -- but not allowed to justify a merge.
GUARD_EXCLUDED_TYPES = frozenset({EntityType.OTHER.value})

#: Share of the extracted corpus above which an entity stops discriminating.
DEFAULT_MAX_DOC_FRACTION = 0.10

#: Never exclude an entity seen in fewer articles than this, whatever the fraction says.
#: Without it a young corpus excludes everything -- with 20 articles a 10% ceiling
#: rejects anything appearing twice, and nothing would ever cluster.
DEFAULT_MIN_DOC_FLOOR = 5


def overexposure_threshold(
    db: Session,
    *,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> int:
    """Article count above which an entity is too common to discriminate."""
    corpus = (
        db.scalar(
            select(func.count(RawArticle.id)).where(RawArticle.entities_extracted_at.is_not(None))
        )
        or 0
    )
    return max(min_floor, math.ceil(corpus * max_fraction))


def _exposure_groups(named: list[tuple[int, str, str]]) -> dict[int, int]:
    """Map each entity to the id whose exposure it shares.

    "Donald Trump" and "Trump" are one person, stored as two rows. At 393 articles the
    ceiling was 40: `Trump` appeared in 97 and was correctly excluded, while
    `Donald Trump` appeared in 18 and was admitted. The same entity was both blocked and
    allowed depending on which surface form an article happened to use, so the ceiling
    leaked under an alias -- and that costs PRECISION, which is the one thing the guard
    exists to protect.

    The rule is deliberately narrow: same type, and one name is a whole-word SUFFIX of
    the other. It fires only when the corpus actually contains both forms, so
    "John Smith" and "Jane Smith" are not grouped -- neither is a suffix of the other,
    and nothing named "Smith" need exist.

    Whole-word, not substring: "Ian" must not absorb "Iran".

    This changes only how exposure is COUNTED. It does not claim the rows are the same
    entity, does not merge them, and cannot cause a wrong join -- its only effect is to
    exclude more, which is the safe direction (ADR-0015).
    """
    by_key: dict[tuple[str, str], int] = {}
    for entity_id, name, kind in named:
        by_key.setdefault((kind, " ".join(name.lower().split())), entity_id)

    group: dict[int, int] = {}
    for entity_id, name, kind in named:
        tokens = name.lower().split()
        target = entity_id
        # Longest suffix first: "Donald Trump" prefers "Trump" over nothing, and a
        # three-part name prefers the two-part form it actually contains.
        for start in range(1, len(tokens)):
            candidate = by_key.get((kind, " ".join(tokens[start:])))
            if candidate is not None and candidate != entity_id:
                target = candidate
                break
        group[entity_id] = target
    return group


def overexposed_entity_ids(
    db: Session,
    *,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> set[int]:
    """Entities that appear in too many articles to mean anything on their own.

    Exposure is counted per GROUP, not per row, so an over-exposed name cannot slip
    through under a longer form of itself -- see `_exposure_groups`.
    """
    threshold = overexposure_threshold(db, max_fraction=max_fraction, min_floor=min_floor)

    rows = db.execute(
        select(
            Entity.id,
            Entity.canonical_name,
            Entity.entity_type,
            func.count(func.distinct(RawArticleEntity.raw_article_id)).label("df"),
        )
        .join(RawArticleEntity, RawArticleEntity.entity_id == Entity.id)
        .group_by(Entity.id, Entity.canonical_name, Entity.entity_type)
    ).all()

    group = _exposure_groups([(r.id, r.canonical_name, r.entity_type) for r in rows])

    totals: dict[int, int] = {}
    for row in rows:
        totals[group[row.id]] = totals.get(group[row.id], 0) + int(row.df)

    return {row.id for row in rows if totals[group[row.id]] > threshold}


#: Domain labels that are never a publisher's identity. Without this, "com" or "org"
#: would be compared against entity names, which no entity is called but which costs
#: nothing to exclude.
_DOMAIN_NOISE = frozenset({"com", "org", "net", "gov", "edu", "co", "uk", "us", "io", "news"})


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _publisher_labels(domain: str) -> set[str]:
    """The words a publisher is likely to call itself, from its hostname.

    `npr.org` -> {"npr"}; `science.nasa.gov` -> {"science", "nasa"}. The source's `name`
    is set to its hostname at auto-creation, so the hostname is all there is to go on
    until a human classifies it.
    """
    labels = {
        label
        for label in domain.lower().removeprefix("www.").split(".")
        if label and label not in _DOMAIN_NOISE
    }
    # Normalised the same way entity names are, or "ap-news" would never match
    # "AP News". Comparing a raw hostname label against a cleaned entity name silently
    # matches nothing, which looks exactly like the filter being unnecessary.
    return {_normalise(label) for label in labels if _normalise(label)}


def guard_entity_ids(
    db: Session,
    raw_article_id: int,
    *,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> set[int]:
    """The entities of one article that are allowed to license a join.

    An article whose entities are all common, all OTHER, or all its own masthead
    returns an empty set, and therefore cannot join anything. That is the correct
    outcome: nothing about it distinguishes which event it belongs to.
    """
    excluded = overexposed_entity_ids(db, max_fraction=max_fraction, min_floor=min_floor)

    domain = db.scalar(
        select(Source.domain)
        .join(RawArticle, RawArticle.source_id == Source.id)
        .where(RawArticle.id == raw_article_id)
    )
    publisher = _publisher_labels(domain or "")

    rows = db.execute(
        select(RawArticleEntity.entity_id, Entity.canonical_name)
        .join(Entity, Entity.id == RawArticleEntity.entity_id)
        .where(
            RawArticleEntity.raw_article_id == raw_article_id,
            Entity.entity_type.not_in(GUARD_EXCLUDED_TYPES),
        )
    ).all()

    return {
        entity_id
        for entity_id, name in rows
        if entity_id not in excluded and _normalise(name) not in publisher
    }


def shared_guard_entities(
    db: Session,
    raw_article_id: int,
    story_id: int,
    *,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> set[int]:
    """Discriminative entities an article and a story have in common.

    Non-empty is what satisfies the guard. The caller still applies the similarity
    threshold -- both conditions are required, and neither substitutes for the other.

    The story side goes through `story_guard_entities` rather than a raw StoryEntity
    query, so the LIVE join decision gets the same publisher filter consolidation and
    the recall diagnostic do. Querying StoryEntity directly here is what let a
    single-publisher story's own masthead count as a shared entity in the first place.
    """
    article = guard_entity_ids(db, raw_article_id, max_fraction=max_fraction, min_floor=min_floor)
    if not article:
        return set()

    story = story_guard_entities(db, story_id, max_fraction=max_fraction, min_floor=min_floor)
    return article & story


# --------------------------------------------------------------- join or create

#: PIPELINE.md §6 defaults. Overridden from config by the caller; repeated here so this
#: module is usable from a script or a test without constructing Settings.
DEFAULT_JOIN_THRESHOLD = 0.82
DEFAULT_WINDOW_HOURS = 48
DEFAULT_CANDIDATE_LIMIT = 10


@dataclass(frozen=True)
class Decision:
    """What happened to one article, and why.

    `similarity` and `shared_entities` are recorded rather than recomputed: the centroid
    moves as members are added, so the numbers that justified a join are not
    reproducible afterwards. Keeping them is what makes a cluster auditable.
    """

    article_id: int
    story_id: int
    joined: bool
    similarity: float | None = None
    shared_entities: int = 0
    reason: str = ""


def pending_clustering_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count(RawArticle.id)).where(
                RawArticle.story_id.is_(None),
                RawArticle.embedding.is_not(None),
                RawArticle.entities_extracted_at.is_not(None),
            )
        )
        or 0
    )


def _candidates(
    db: Session, embedding: list[float], *, window_hours: int, limit: int
) -> list[tuple[int, float]]:
    """(story_id, cosine similarity) for the nearest recently-active stories.

    The time filter comes first so the scan is bounded by activity rather than by the
    whole table. `<=>` is cosine DISTANCE -- 0 identical, 1 orthogonal -- so similarity
    is `1 - distance`, verified against the server rather than assumed.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    distance = Story.centroid.cosine_distance(embedding)
    rows = db.execute(
        select(Story.id, distance.label("distance"))
        .where(Story.centroid.is_not(None), Story.last_activity_at >= cutoff)
        .order_by(distance)
        .limit(limit)
    ).all()
    return [(story_id, 1.0 - float(dist)) for story_id, dist in rows]


def _promote_entities(db: Session, article_id: int, story_id: int) -> None:
    """Copy an article's entities onto its story.

    ALL of them, not just the discriminative ones. The guard filters at read time, and
    an entity that is too common today may not be next month -- storing only what
    currently passes would bake one moment's corpus statistics into the data.
    """
    rows = db.execute(
        select(RawArticleEntity.entity_id, RawArticleEntity.mention_count).where(
            RawArticleEntity.raw_article_id == article_id
        )
    ).all()
    for entity_id, mentions in rows:
        db.execute(
            pg_insert(StoryEntity)
            .values(story_id=story_id, entity_id=entity_id, mention_count=mentions or 0)
            .on_conflict_do_nothing(constraint="uq_story_entities_pair")
        )


def resync_story_entities(db: Session, story_id: int) -> None:
    """Recompute a story's entire StoryEntity set from its CURRENT members' CURRENT
    entities. Unlike `_promote_entities`, this can remove rows, not just add them.

    FOUND IN PRODUCTION: `_promote_entities` runs once, at the moment an article joins
    a story. Nothing kept `StoryEntity` in sync afterwards -- so when an article that
    had already joined a story was later re-extracted (a backfill after fixing entity
    normalisation, or any future re-extraction of an already-clustered article),
    `store_entities` replaced that article's `raw_article_entities` but the story's
    promoted copy kept its stale snapshot. A Nepal-floods story ended up with "United
    States" in its guard set though none of its eight current members carried that
    entity any more -- a ghost with no article behind it.

    That is not cosmetic. `story_guard_entities` reads this same table for the LIVE
    join decision, so a ghost entity could license a future wrong join on the strength
    of something no current member actually says.

    Call this whenever a member article's entities change after the story already
    exists -- currently: when `store_entities` re-extracts an article that already
    belongs to a story. Deliberately a full DELETE-then-rebuild rather than a diff:
    the failure mode being fixed is exactly "a stale row nobody noticed", so the fix
    must not leave room for a different stale row to survive it.
    """
    rows = db.execute(
        select(RawArticleEntity.entity_id, func.sum(RawArticleEntity.mention_count))
        .join(StorySource, StorySource.raw_article_id == RawArticleEntity.raw_article_id)
        .where(StorySource.story_id == story_id)
        .group_by(RawArticleEntity.entity_id)
    ).all()

    db.execute(delete(StoryEntity).where(StoryEntity.story_id == story_id))
    for entity_id, mentions in rows:
        db.execute(
            pg_insert(StoryEntity).values(
                story_id=story_id, entity_id=entity_id, mention_count=int(mentions or 0)
            )
        )
    db.flush()


def _recount_sources(db: Session, story_id: int) -> None:
    """Refresh `source_count` from the membership.

    `independent_source_count` is deliberately NOT derived here. ADR-0013 is explicit
    that nothing may infer independence from source identity -- forty outlets carrying
    one wire story are forty sources and one witness. Setting it to the distinct-source
    count would be exactly that inference, and a corroboration rule downstream would
    then read a number that means something else.
    """
    count = db.scalar(
        select(func.count(func.distinct(RawArticle.source_id)))
        .select_from(StorySource)
        .join(RawArticle, RawArticle.id == StorySource.raw_article_id)
        .where(StorySource.story_id == story_id)
    )
    db.execute(update(Story).where(Story.id == story_id).values(source_count=count or 0))


def _update_centroid(db: Session, story_id: int, embedding: list[float]) -> None:
    """Running mean over the story's members.

    Not normalised: cosine distance ignores magnitude, so normalising would cost a pass
    over 384 floats to change nothing. Computed from the member count so the mean is
    exact rather than an exponential approximation that drifts with arrival order.
    """
    story = db.get(Story, story_id)
    if story is None:
        return
    members = (
        db.scalar(select(func.count(StorySource.id)).where(StorySource.story_id == story_id)) or 0
    )
    if story.centroid is None or members <= 1:
        story.centroid = list(embedding)
        return
    previous = list(story.centroid)
    n = members - 1
    story.centroid = [(previous[i] * n + embedding[i]) / members for i in range(len(embedding))]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _spans_unrelated_stories(
    db: Session,
    matches: list[tuple[int, float, set[int]]],
    *,
    join_threshold: float,
) -> bool:
    """Whether an article qualifies to join two stories that are not about each other.

    This is what a digest looks like from the inside. NPR publishes an "Up First" every
    morning covering several unrelated events; it is similar enough to each, and shares
    a discriminative entity with each, so it passes the guard against all of them. It
    joined the ICE story in the first real run while the shutdown it also covered was a
    separate story two rows down.

    Letting it join is worse than it sounds. A digest is not a second SOURCE for a
    story -- it is a paragraph in a summary -- and a later verification step counting
    members would read it as corroboration.

    Two stories that are themselves similar are not evidence of a digest; they are one
    event that clustering has under-split, and the article should join the better of
    them. So the test is mutual DISsimilarity between the matched stories, using the
    same threshold: if they are close enough that they would merge with each other,
    they are one story.
    """
    if len(matches) < 2:
        return False

    centroids: dict[int, list[float]] = {}
    for story_id, _, _ in matches:
        story = db.get(Story, story_id)
        if story is not None and story.centroid is not None:
            centroids[story_id] = list(story.centroid)

    ids = list(centroids)
    for i, first in enumerate(ids):
        for second in ids[i + 1 :]:
            if _cosine(centroids[first], centroids[second]) < join_threshold:
                return True
    return False


def cluster_article(
    db: Session,
    article: RawArticle,
    *,
    join_threshold: float = DEFAULT_JOIN_THRESHOLD,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> Decision:
    """Join one article to a story, or start a new one (PIPELINE.md §6).

    BOTH conditions are required to join: cosine similarity at or above the threshold,
    AND at least one shared discriminative entity. Neither substitutes for the other.
    Similarity alone merges "shooting in Ohio" with "shooting in Nevada"; entities alone
    merge every article that mentions the same senator.

    Failing to join is never an error. It creates a story, which is over-splitting --
    the safe direction under ADR-0015, because a duplicate story is visible and
    mergeable while a story asserting facts about the wrong event is neither.
    """
    embedding = list(article.embedding or [])
    if not embedding:
        raise ValueError(f"article {article.id} has no embedding")

    matches: list[tuple[int, float, set[int]]] = []
    for story_id, similarity in _candidates(
        db, embedding, window_hours=window_hours, limit=candidate_limit
    ):
        if similarity < join_threshold:
            # Candidates come back ordered by distance, so once one is below the
            # threshold every later one is too.
            break
        overlap = shared_guard_entities(
            db, article.id, story_id, max_fraction=max_fraction, min_floor=min_floor
        )
        if overlap:
            matches.append((story_id, similarity, overlap))

    digest = _spans_unrelated_stories(db, matches, join_threshold=join_threshold)

    best_id: int | None = None
    best_similarity = 0.0
    shared: set[int] = set()
    if matches and not digest:
        best_id, best_similarity, shared = matches[0]

    now = datetime.now(UTC)

    if best_id is None:
        story = Story(
            title=article.title,
            centroid=list(embedding),
            first_seen_at=now,
            last_activity_at=now,
        )
        db.add(story)
        db.flush()
        db.add(
            StorySource(
                story_id=story.id,
                raw_article_id=article.id,
                similarity=None,
                is_primary=True,
            )
        )
        db.flush()
        article.story_id = story.id
        _promote_entities(db, article.id, story.id)
        _recount_sources(db, story.id)
        return Decision(
            article_id=article.id,
            story_id=story.id,
            joined=False,
            reason=(
                "spans unrelated stories; treated as a digest"
                if digest
                else "no candidate cleared both the similarity threshold and the entity guard"
            ),
        )

    db.add(
        StorySource(
            story_id=best_id,
            raw_article_id=article.id,
            similarity=round(best_similarity, 4),
            is_primary=False,
        )
    )
    db.flush()
    article.story_id = best_id
    _promote_entities(db, article.id, best_id)
    _update_centroid(db, best_id, embedding)
    _recount_sources(db, best_id)
    db.execute(update(Story).where(Story.id == best_id).values(last_activity_at=now))

    return Decision(
        article_id=article.id,
        story_id=best_id,
        joined=True,
        similarity=round(best_similarity, 4),
        shared_entities=len(shared),
        reason="similarity and shared discriminative entity",
    )


def cluster_pending(
    db: Session,
    *,
    limit: int = 200,
    join_threshold: float = DEFAULT_JOIN_THRESHOLD,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> list[Decision]:
    """Cluster articles that are ready, oldest first.

    Ready means embedded AND extracted: an article missing either cannot be judged, and
    clustering it on what is available would decide with half the evidence. Oldest first
    because a story should be founded by the first article about it, not by whichever
    happened to be processed first.
    """
    articles = db.scalars(
        select(RawArticle)
        .where(
            RawArticle.story_id.is_(None),
            RawArticle.embedding.is_not(None),
            RawArticle.entities_extracted_at.is_not(None),
        )
        .order_by(RawArticle.published_at, RawArticle.id)
        .limit(limit)
    ).all()

    decisions = [
        cluster_article(
            db,
            article,
            join_threshold=join_threshold,
            window_hours=window_hours,
            candidate_limit=candidate_limit,
            max_fraction=max_fraction,
            min_floor=min_floor,
        )
        for article in articles
    ]
    if decisions:
        joined = sum(1 for d in decisions if d.joined)
        logger.info(
            "clustered articles",
            extra={"articles": len(decisions), "joined": joined, "new": len(decisions) - joined},
        )
    return decisions


# ------------------------------------------------------------------ consolidation

#: Stricter than the join threshold, and deliberately so. Adding one article to a story
#: risks one wrong member; merging two stories asserts that everything already in both
#: is one event, so it should need more evidence, not the same.
DEFAULT_MERGE_THRESHOLD = 0.90


def story_guard_entities(
    db: Session,
    story_id: int,
    *,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> set[int]:
    """A story's entities that are allowed to justify a merge.

    The same filters the article-side guard applies. Consolidation must not become a
    way around the guard: if "United States" cannot license a join, it cannot license
    a merge either, or every US story eventually collapses into one.

    FOUND IN PRODUCTION: this originally had no publisher filter at all, on the theory
    that "a story has no single publisher". True for a genuinely multi-outlet story,
    false for a singleton -- which has exactly one -- and a singleton founded by, say,
    an npr.org article had "NPR" itself sitting in its own guard set, unfiltered.
    Nothing had yet exploited it, but the live join path (`shared_guard_entities`, via
    the raw StoryEntity query it used before this fix) was exposed to the same gap:
    a second outlet's article merely mentioning "NPR reported..." could have joined a
    story on the strength of the first outlet naming itself.

    The filter applies only while every CURRENT member shares one publisher, and lifts
    the moment a second, different outlet joins: at that point the entity is no longer
    self-attribution for every member, and excluding it would discard a genuine
    signal -- one outlet naming another as its actual subject.
    """
    excluded = overexposed_entity_ids(db, max_fraction=max_fraction, min_floor=min_floor)

    domains = set(
        db.execute(
            select(Source.domain)
            .join(RawArticle, RawArticle.source_id == Source.id)
            .join(StorySource, StorySource.raw_article_id == RawArticle.id)
            .where(StorySource.story_id == story_id)
        ).scalars()
    )
    # Only ever non-empty when the story is currently single-publisher -- see the
    # docstring for why a multi-outlet story gets no publisher filtering at all.
    publisher = _publisher_labels(next(iter(domains))) if len(domains) == 1 else set()

    rows = db.execute(
        select(StoryEntity.entity_id, Entity.canonical_name)
        .join(Entity, Entity.id == StoryEntity.entity_id)
        .where(
            StoryEntity.story_id == story_id,
            Entity.entity_type.not_in(GUARD_EXCLUDED_TYPES),
        )
    ).all()

    return {
        entity_id
        for entity_id, name in rows
        if entity_id not in excluded and _normalise(name) not in publisher
    }


def merge_stories(db: Session, survivor_id: int, absorbed_id: int) -> None:
    """Move everything from one story into another and record where it went.

    The absorbed row is KEPT, with `merged_into_id` set. PIPELINE.md requires merges to
    be auditable, and a deleted row cannot explain where its articles went -- someone
    looking at an article's history would find a dangling id and no account of it.
    """
    db.execute(
        update(StorySource).where(StorySource.story_id == absorbed_id).values(story_id=survivor_id)
    )
    db.execute(
        update(RawArticle).where(RawArticle.story_id == absorbed_id).values(story_id=survivor_id)
    )

    # Entities move by insert-if-absent rather than UPDATE: the pair is unique, so an
    # entity both stories already had would violate the constraint on a blind update.
    for entity_id, mentions in db.execute(
        select(StoryEntity.entity_id, StoryEntity.mention_count).where(
            StoryEntity.story_id == absorbed_id
        )
    ).all():
        db.execute(
            pg_insert(StoryEntity)
            .values(story_id=survivor_id, entity_id=entity_id, mention_count=mentions or 0)
            .on_conflict_do_nothing(constraint="uq_story_entities_pair")
        )
    db.execute(delete(StoryEntity).where(StoryEntity.story_id == absorbed_id))

    db.execute(
        update(Story)
        .where(Story.id == absorbed_id)
        .values(merged_into_id=survivor_id, centroid=None)
    )
    db.flush()

    # Recompute rather than average the two centroids: the members are known, so the
    # exact mean is available and an average of averages would weight a two-article
    # story the same as a twenty-article one.
    members = (
        db.execute(
            select(RawArticle.embedding)
            .join(StorySource, StorySource.raw_article_id == RawArticle.id)
            .where(StorySource.story_id == survivor_id, RawArticle.embedding.is_not(None))
        )
        .scalars()
        .all()
    )
    if members:
        vectors = [list(v) for v in members]
        survivor = db.get(Story, survivor_id)
        if survivor is not None:
            survivor.centroid = [sum(col) / len(vectors) for col in zip(*vectors, strict=True)]

    _recount_sources(db, survivor_id)
    db.execute(
        update(Story).where(Story.id == survivor_id).values(last_activity_at=datetime.now(UTC))
    )


@dataclass(frozen=True)
class Merge:
    survivor_id: int
    absorbed_id: int
    similarity: float
    shared_entities: int


def consolidate_stories(
    db: Session,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    max_merges: int = 50,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> list[Merge]:
    """Merge stories that are the same event, within the recent window.

    The counterweight to a design that leans towards over-splitting. Join-or-create
    refuses whenever it is unsure, and the digest rule refuses again; both produce
    duplicate stories for one event. Nothing else puts them back together.

    Conditions are the join conditions, tightened: centroid similarity at or above
    `merge_threshold` (higher than the join threshold -- merging asserts that everything
    already in both stories is one event) AND a shared discriminative entity. The guard
    applies here for the same reason it applies at join time, and skipping it would make
    consolidation a way around it.

    The older story survives, so a story keeps the identity it was founded with rather
    than being renamed by whichever duplicate happened to grow faster.

    Deliberately NOT HDBSCAN, which PIPELINE.md §6 names. Density clustering
    re-partitions a whole space, which is the right tool for splitting a cluster whose
    intra-similarity collapsed -- a different problem, needing a model, belonging on the
    desktop (ADR-0015). Merging known duplicates is pairwise and needs no model, so it
    runs here where the data is.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    stories = db.execute(
        select(Story.id, Story.centroid, Story.first_seen_at)
        .where(
            Story.centroid.is_not(None),
            Story.merged_into_id.is_(None),
            Story.last_activity_at >= cutoff,
        )
        .order_by(Story.first_seen_at, Story.id)
    ).all()

    merges: list[Merge] = []
    absorbed: set[int] = set()

    for index, (story_id, centroid, _) in enumerate(stories):
        if story_id in absorbed or len(merges) >= max_merges:
            continue
        survivor_entities = story_guard_entities(
            db, story_id, max_fraction=max_fraction, min_floor=min_floor
        )
        if not survivor_entities:
            continue

        for other_id, other_centroid, _ in stories[index + 1 :]:
            if other_id in absorbed or len(merges) >= max_merges:
                continue
            similarity = _cosine(list(centroid), list(other_centroid))
            if similarity < merge_threshold:
                continue
            shared = survivor_entities & story_guard_entities(
                db, other_id, max_fraction=max_fraction, min_floor=min_floor
            )
            if not shared:
                continue

            merge_stories(db, story_id, other_id)
            absorbed.add(other_id)
            merges.append(
                Merge(
                    survivor_id=story_id,
                    absorbed_id=other_id,
                    similarity=round(similarity, 4),
                    shared_entities=len(shared),
                )
            )

    if merges:
        logger.info("consolidated stories", extra={"merges": len(merges)})
    return merges


@dataclass(frozen=True)
class Rejoin:
    survivor_id: int
    absorbed_id: int
    similarity: float
    shared_entities: int


def rejoin_stragglers(
    db: Session,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    join_threshold: float = DEFAULT_JOIN_THRESHOLD,
    max_rejoins: int = 50,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> list[Rejoin]:
    """Reunite a singleton story with a larger story it should have joined, at the
    ORIGINAL join threshold -- not consolidation's stricter merge_threshold.

    A gap `consolidate_stories` cannot close. A pair can miss the live join path for
    reasons that have nothing to do with whether they are the same event: the digest
    rule declining a story-spanning article, or two near-duplicate articles landing in
    the same dispatch batch before either existed as a candidate for the other (FOUND
    IN PRODUCTION: two singleton "Iran fires on its Gulf neighbors" stories, articles
    discovered under four hours apart, that never got a chance to be compared). Once a
    pair becomes two separate stories, `consolidate_stories` is the only thing that
    puts stories back together -- and its threshold (0.90) is DELIBERATELY higher than
    the join threshold (0.82), because merging asserts everything already in BOTH
    stories is one event, a stronger claim than a single join ever makes. A pair
    scoring between 0.82 and 0.90 can therefore join a story fresh but can never be
    reunited with one after the fact -- backwards, since the same evidence should
    support the same decision whichever direction it runs.

    Deliberately narrower than lowering `consolidate_stories`' threshold everywhere:

      * only a story with exactly ONE member article is a rejoin candidate. A story
        with two or more has already demonstrated it is a real, distinct cluster, not
        an under-joined straggler -- lowering the bar for that case is exactly what
        `merge_threshold`'s higher bar exists to prevent.
      * a straggler may only join a LARGER story (more member articles). This is "the
        straggler finishes the join it missed", not two arbitrary stories merging into
        whichever happens to be older, which is `consolidate_stories`' rule.
      * the guard and threshold are otherwise identical to a live join: the same
        `story_guard_entities` intersection, `join_threshold` rather than
        `merge_threshold`. If a fresh article with this exact centroid would have
        joined the larger story today, the straggler should too.

    Two singletons never rejoin each other here, even when they are obviously the same
    event (the production example above): neither is "larger", so neither is a valid
    target under this function's own rule. That case is `consolidate_stories`' job, at
    its higher bar -- left there deliberately rather than widened, per the operator's
    explicit choice for a narrow, targeted pass over a general threshold change.

    The larger story survives (unlike `consolidate_stories`, where the older one does):
    a singleton rejoining an established multi-source story is exactly what a live join
    would have produced.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    stories = db.execute(
        select(Story.id, Story.centroid, func.count(StorySource.id).label("article_count"))
        .join(StorySource, StorySource.story_id == Story.id)
        .where(
            Story.centroid.is_not(None),
            Story.merged_into_id.is_(None),
            Story.last_activity_at >= cutoff,
        )
        .group_by(Story.id, Story.centroid)
    ).all()

    singletons = [(sid, centroid) for sid, centroid, count in stories if count == 1]
    non_singletons = [(sid, centroid) for sid, centroid, count in stories if count > 1]

    rejoins: list[Rejoin] = []
    absorbed: set[int] = set()

    for straggler_id, straggler_centroid in singletons:
        if len(rejoins) >= max_rejoins:
            break
        straggler_entities = story_guard_entities(
            db, straggler_id, max_fraction=max_fraction, min_floor=min_floor
        )
        if not straggler_entities:
            continue

        best: tuple[int, float, int] | None = None
        for other_id, other_centroid in non_singletons:
            if other_id in absorbed:
                continue
            similarity = _cosine(list(straggler_centroid), list(other_centroid))
            if similarity < join_threshold:
                continue
            shared = straggler_entities & story_guard_entities(
                db, other_id, max_fraction=max_fraction, min_floor=min_floor
            )
            if not shared:
                continue
            if best is None or similarity > best[1]:
                best = (other_id, similarity, len(shared))

        if best is None:
            continue
        other_id, similarity, shared_count = best
        merge_stories(db, other_id, straggler_id)
        absorbed.add(straggler_id)
        rejoins.append(
            Rejoin(
                survivor_id=other_id,
                absorbed_id=straggler_id,
                similarity=round(similarity, 4),
                shared_entities=shared_count,
            )
        )

    if rejoins:
        logger.info("rejoined stragglers", extra={"rejoins": len(rejoins)})
    return rejoins
