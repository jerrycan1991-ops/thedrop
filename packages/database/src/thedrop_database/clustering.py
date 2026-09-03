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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from thedrop_database.enums import EntityType
from thedrop_database.models import Entity, RawArticle, RawArticleEntity, Source, StoryEntity

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
