"""US relevance scoring (PIPELINE.md §7).

PIPELINE.md specifies five weighted signals:

    US entities (people, orgs, places, agencies)     0.30
    US publisher share in cluster                    0.20
    Topic class US-salience                          0.20
    Direct impact on US audiences                     0.20
    US search/trend signal presence                   0.10

Only the first two are implemented here. The other three either need an external API
this project has no integration for (search/trend) or are content judgements a
structured query cannot honestly make (topic class, direct impact) -- CLAUDE.md forbids
fabricating a signal, and a hand-built keyword heuristic standing in for "is this about
domestic policy" would be exactly that, dressed up as data. Building those properly
(most plausibly a Claude-based classifier, following ADR-0008's untrusted-content
rules) is real, separate work.

Rather than leave the score capped at 50 -- which would make an unmistakably American
story and a barely-American one both read as "middling" on a 0-100 scale, for no reason
a viewer of the score could see -- entities and publisher share are RESCALED to the full
weight between them (0.30/0.50 = 0.60, 0.20/0.50 = 0.40) and the score is genuinely
computed on a 0-100 scale from what is actually measured. `us_relevance_basis` is what
keeps this honest: `coverage: 0.50` records that only half the documented formula ran,
so nothing downstream mistakes a rescaled partial score for the complete one.

NOT WIRED TO ANY GATE. PIPELINE.md ties `US_RELEVANCE_MIN` to whether a story gets
written, but nothing writes stories yet -- Phase 4 does not exist. Building a gate with
no consumer would be exactly the kind of premature abstraction CLAUDE.md warns against;
whoever builds Phase 4 decides how a partial-coverage score should be treated at the
gate, informed by `coverage`.

Runs on the VPS. Like clustering (ADR-0015), this needs a database and needs no model --
CLAUDE.md's resource discipline is about ML runtimes, not about SQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from thedrop_database.enums import EntityType
from thedrop_database.models import Entity, RawArticle, Source, Story, StoryEntity, StorySource

logger = logging.getLogger(__name__)

#: Weight of each signal in the DOCUMENTED five-signal formula (PIPELINE.md §7), not
#: renormalised. `WEIGHT_ENTITIES + WEIGHT_PUBLISHER_SHARE` is the total weight this
#: module actually covers; see `_COVERAGE` below.
WEIGHT_ENTITIES = 0.30
WEIGHT_PUBLISHER_SHARE = 0.20

_COVERAGE = WEIGHT_ENTITIES + WEIGHT_PUBLISHER_SHARE  # 0.50
_RESCALED_ENTITIES = WEIGHT_ENTITIES / _COVERAGE  # 0.60
_RESCALED_PUBLISHER_SHARE = WEIGHT_PUBLISHER_SHARE / _COVERAGE  # 0.40

#: US states, DC, the country itself, and unambiguous federal institutions. Small and
#: explicit on purpose, the same shape as `_STATE_ABBREVIATIONS` in agent/entities.py:
#: a curated list of public facts is not a fabricated signal, but it is also not a
#: complete gazetteer, and it must never claim to be one. A story about a topic that
#: happens to name none of these scores 0 on this signal, which is a true statement
#: about what was found, not a claim that the story is not American.
US_ENTITY_MARKERS: frozenset[str] = frozenset(
    {
        "united states",
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington state",
        "west virginia",
        "wisconsin",
        "wyoming",
        "washington, d.c",
        "washington dc",
        "district of columbia",
        "congress",
        "house of representatives",
        "senate",
        "white house",
        "supreme court",
        "pentagon",
        "federal reserve",
        "fbi",
        "cia",
        "irs",
        "sec",
        "ftc",
        "doj",
        "ice",
        "usps",
        "nasa",
        "epa",
        "cdc",
        "fda",
    }
)


def _normalise(name: str) -> str:
    return " ".join(name.lower().split())


@dataclass(frozen=True)
class ScoreResult:
    score: int
    entity_signal: float
    publisher_signal: float
    matched_entities: list[str] = field(default_factory=list)
    us_sources: int = 0
    total_sources: int = 0
    coverage: float = _COVERAGE

    def basis(self) -> dict[str, object]:
        """What `us_relevance_basis` stores. Kept separate from the dataclass fields
        so the stored shape can evolve without renaming what callers read."""
        return {
            "formula_version": 1,
            "coverage": self.coverage,
            "signals": {
                "us_entities": {
                    "weight": WEIGHT_ENTITIES,
                    "value": round(self.entity_signal, 4),
                    "matched": self.matched_entities,
                },
                "us_publisher_share": {
                    "weight": WEIGHT_PUBLISHER_SHARE,
                    "value": round(self.publisher_signal, 4),
                    "us_sources": self.us_sources,
                    "total_sources": self.total_sources,
                },
            },
            "signals_not_implemented": [
                "topic_class_us_salience",
                "direct_impact_on_us_audiences",
                "us_search_trend_signal",
            ],
        }


def _entity_signal(db: Session, story_id: int) -> tuple[float, list[str]]:
    """Share of the story's non-generic entities that are unambiguously American.

    OTHER-typed entities are excluded for the same reason the clustering guard excludes
    them: that is where the tagger's noise lands (nationality adjectives, truncated
    titles), and noise should not move a score any more than it should license a join.
    """
    rows = (
        db.execute(
            select(Entity.canonical_name)
            .join(StoryEntity, StoryEntity.entity_id == Entity.id)
            .where(StoryEntity.story_id == story_id, Entity.entity_type != EntityType.OTHER.value)
        )
        .scalars()
        .all()
    )

    if not rows:
        return 0.0, []

    matched = sorted({name for name in rows if _normalise(name) in US_ENTITY_MARKERS})
    return len(matched) / len(rows), matched


def _publisher_signal(db: Session, story_id: int) -> tuple[float, int, int]:
    """Share of the story's member articles published by a US-classified source."""
    rows = (
        db.execute(
            select(Source.country)
            .join(RawArticle, RawArticle.source_id == Source.id)
            .join(StorySource, StorySource.raw_article_id == RawArticle.id)
            .where(StorySource.story_id == story_id)
        )
        .scalars()
        .all()
    )

    if not rows:
        return 0.0, 0, 0

    us = sum(1 for country in rows if country == "US")
    return us / len(rows), us, len(rows)


def score_us_relevance(db: Session, story_id: int) -> ScoreResult:
    """Compute the two implemented signals and combine them, rescaled to 0-100.

    Does not write anything -- see `update_us_relevance` for the version that does.
    Separated so the computation is testable without touching the database, and so a
    caller that only wants the number (without committing) has that option.
    """
    entity_signal, matched = _entity_signal(db, story_id)
    publisher_signal, us_sources, total_sources = _publisher_signal(db, story_id)

    raw = entity_signal * _RESCALED_ENTITIES + publisher_signal * _RESCALED_PUBLISHER_SHARE
    score = round(raw * 100)

    return ScoreResult(
        score=score,
        entity_signal=entity_signal,
        publisher_signal=publisher_signal,
        matched_entities=matched,
        us_sources=us_sources,
        total_sources=total_sources,
    )


def update_us_relevance(db: Session, story_id: int) -> ScoreResult:
    """Compute and persist. The caller commits.

    Flushes before returning, matching `merge_stories` and `cluster_article`: a caller
    that immediately re-queries the story within the same transaction must see the
    write, not the value that was there before it.
    """
    result = score_us_relevance(db, story_id)
    story = db.get(Story, story_id)
    if story is not None:
        story.us_relevance_score = result.score
        story.us_relevance_basis = result.basis()
        story.scores_computed_at = datetime.now(UTC)
        db.flush()
    return result


def unscored_story_ids(db: Session, *, limit: int) -> list[int]:
    """Unmerged stories with no score yet, oldest first.

    Every story has at least one `story_sources` row by construction -- both the join
    and the create path in `cluster_article` insert one -- so this does not re-check
    for members the way `_singleton_story_ids` in clustering.py checks for a COUNT.
    """
    return list(
        db.scalars(
            select(Story.id)
            .where(Story.merged_into_id.is_(None), Story.us_relevance_score.is_(None))
            .order_by(Story.first_seen_at, Story.id)
            .limit(limit)
        ).all()
    )
