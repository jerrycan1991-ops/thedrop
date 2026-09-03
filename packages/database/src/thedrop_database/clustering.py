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

from sqlalchemy import func, select, update
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


def overexposed_entity_ids(
    db: Session,
    *,
    max_fraction: float = DEFAULT_MAX_DOC_FRACTION,
    min_floor: int = DEFAULT_MIN_DOC_FLOOR,
) -> set[int]:
    """Entities that appear in too many articles to mean anything on their own."""
    threshold = overexposure_threshold(db, max_fraction=max_fraction, min_floor=min_floor)
    rows = db.execute(
        select(RawArticleEntity.entity_id)
        .group_by(RawArticleEntity.entity_id)
        .having(func.count(func.distinct(RawArticleEntity.raw_article_id)) > threshold)
    ).scalars()
    return set(rows)


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
    """
    article = guard_entity_ids(db, raw_article_id, max_fraction=max_fraction, min_floor=min_floor)
    if not article:
        return set()

    story = set(
        db.execute(select(StoryEntity.entity_id).where(StoryEntity.story_id == story_id)).scalars()
    )
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
