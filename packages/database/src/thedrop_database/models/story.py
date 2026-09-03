"""Stories: one real-world event, many source articles.

A `raw_article` is what one publisher said. A `story` is the event they were all
writing about. Clustering is what turns the first into the second, and every score,
every verification decision and eventually every published article hangs off the story
rather than off any single source's account of it.

`centroid` is the running mean of its members' embeddings, in the one 384-dimension
space ADR-0005 established. Incremental clustering runs on the VPS (ADR-0015): the
desktop holds no database credentials, so it cannot run `ORDER BY centroid <=> $1`.

`story_entities` is not decoration. PIPELINE.md §6 requires a shared salient entity
before two articles may join, because embeddings alone happily merge "shooting in Ohio"
with "shooting in Nevada". That is a correctness guard, and it is why `entities` lands
in the same migration as `stories`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import EntitySensitivity, EntityType, RiskTier, StoryStatus


class Story(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """A clustered real-world event."""

    __tablename__ = "stories"
    __table_args__ = (
        # The work queue: what is at each stage, most recently active first.
        Index("ix_stories_status_activity", "status", "last_activity_at"),
        # The clustering candidate query filters on recent activity before it ever
        # touches the vector index, so this carries most of that predicate.
        Index("ix_stories_last_activity", "last_activity_at"),
        # HNSW on `centroid` is deliberately NOT created here. With a handful of rows a
        # sequential scan is faster, index build cost is paid on a 4-core VPS, and the
        # right `m`/`ef_construction` depend on the row count -- which is currently
        # zero. Added in its own migration once the table has a realistic size, the same
        # judgement made for `raw_articles` (see a1c7e2b40f13).
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )

    #: Running mean of member embeddings. Null until the first member is embedded.
    centroid: Mapped[list[float] | None] = mapped_column(Vector(384))

    # No index=True: ix_stories_status_activity leads with status, so a standalone
    # index on it would be a second copy of the same prefix.
    status: Mapped[str] = mapped_column(String(16), default=StoryStatus.DISCOVERED, nullable=False)

    #: Distinct `sources`, and the subset judged independent. They differ because forty
    #: outlets carrying one wire story are forty sources and one witness -- ADR-0013.
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    independent_source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    us_relevance_score: Mapped[int | None] = mapped_column(SmallInteger)
    viral_score: Mapped[int | None] = mapped_column(SmallInteger)
    opportunity_score: Mapped[int | None] = mapped_column(SmallInteger)
    importance_score: Mapped[int | None] = mapped_column(SmallInteger)
    credibility_score: Mapped[int | None] = mapped_column(SmallInteger)
    verification_confidence: Mapped[int | None] = mapped_column(SmallInteger)
    scores_computed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    risk_tier: Mapped[str] = mapped_column(String(16), default=RiskTier.STANDARD, nullable=False)
    risk_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)

    #: Carried into the evidence packet rather than silently dropped: what the sources
    #: disagree about, and what none of them answered.
    known_unknowns: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    contradictions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    evidence_packet: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: sha256 of the packet, so a generated article can be traced back to the exact
    #: evidence it was written from.
    evidence_packet_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))

    #: Set when this story was merged INTO another. The row is kept rather than
    #: deleted: PIPELINE.md requires merges to be recorded so a story's identity is
    #: auditable, and a deleted row cannot explain where its articles went.
    merged_into_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stories.id", ondelete="SET NULL"), index=True
    )

    rejected_reason: Mapped[str | None] = mapped_column(Text)
    defer_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class StorySource(Base, PrimaryKeyMixin, TimestampMixin):
    """Membership of a raw article in a story, with why it joined.

    `similarity` is recorded rather than recomputed: the centroid moves as members are
    added, so the score that justified a join is not reproducible afterwards. Keeping it
    is what makes a cluster auditable.
    """

    __tablename__ = "story_sources"
    __table_args__ = (
        UniqueConstraint("story_id", "raw_article_id", name="uq_story_sources_story_article"),
        Index("ix_story_sources_article", "raw_article_id"),
    )

    # No index=True: uq_story_sources_story_article leads with story_id.
    story_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stories.id", ondelete="CASCADE"), nullable=False
    )
    raw_article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=False
    )

    similarity: Mapped[float | None] = mapped_column(Numeric(5, 4))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Same wire copy arriving under another masthead. Excluded from
    #: `independent_source_count`, because it is one witness (ADR-0013).
    is_syndicated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    added_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Entity(Base, PrimaryKeyMixin, TimestampMixin):
    """A person, organisation, place or thing that stories are about."""

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("canonical_name", "entity_type", name="uq_entities_name_type"),
        Index("ix_entities_type", "entity_type"),
    )

    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), default=EntityType.OTHER, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    wikidata_id: Mapped[str | None] = mapped_column(String(32))
    is_public_figure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: `elevated` for minors, victims, and anyone whose naming carries extra duty of
    #: care. Stored on the entity so every story inherits it instead of re-deciding.
    sensitivity: Mapped[str] = mapped_column(
        String(16), default=EntitySensitivity.NORMAL, nullable=False
    )


class StoryEntity(Base, PrimaryKeyMixin):
    """Which entities a story is about, and how central each one is.

    This is what the clustering guard reads. Two articles may only join a story when
    they share a salient entity, because embeddings alone happily merge "shooting in
    Ohio" with "shooting in Nevada" (PIPELINE.md §6).
    """

    __tablename__ = "story_entities"
    __table_args__ = (
        UniqueConstraint("story_id", "entity_id", name="uq_story_entities_pair"),
        Index("ix_story_entities_entity", "entity_id"),
    )

    # No index=True: uq_story_entities_pair leads with story_id.
    story_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stories.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    salience: Mapped[float | None] = mapped_column(Numeric(4, 3))
    mention_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RawArticleEntity(Base, PrimaryKeyMixin):
    """Entities found in ONE article, before it belongs to any story.

    `story_entities` cannot serve this. The clustering guard has to compare the
    incoming article's entities against a candidate story's entities *before* deciding
    whether the article joins it -- so at the moment the comparison happens, the article
    has no story. This is the article side of that comparison.

    It resolves to the same `entities` rows the story side uses, so "Jerome Powell" is
    one entity whether it was seen in an article or promoted onto a story. Matching on
    entity_id rather than on strings is what keeps the guard from turning into
    approximate name comparison.
    """

    __tablename__ = "raw_article_entities"
    __table_args__ = (
        UniqueConstraint("raw_article_id", "entity_id", name="uq_raw_article_entities_pair"),
        Index("ix_raw_article_entities_entity", "entity_id"),
    )

    # No index=True: uq_raw_article_entities_pair leads with raw_article_id.
    raw_article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    #: Share of this article's entity mentions that were this entity. Centrality, not
    #: model confidence -- a name mentioned once in passing should not gate a merge.
    salience: Mapped[float | None] = mapped_column(Numeric(4, 3))
    mention_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ClusterLabel(Base, PrimaryKeyMixin):
    """A human verdict on one clustering decision.

    Ground truth for the Phase 3 exit criterion: precision >= 0.90 on a hand-labelled
    set. One row per PLACEMENT -- an article joining a story -- because that is the
    decision the guard actually makes. A founder is not a placement and is not labelled;
    counting it would inflate precision with decisions nobody took.

    Kept in the database rather than a file so it survives a redeploy, can be joined
    against the stories it judges, and cannot be silently regenerated. Labels are
    evidence; regenerating them would be inventing them.
    """

    __tablename__ = "cluster_labels"
    __table_args__ = (
        UniqueConstraint("story_id", "raw_article_id", name="uq_cluster_labels_placement"),
        Index("ix_cluster_labels_article", "raw_article_id"),
    )

    # No index=True: uq_cluster_labels_placement leads with story_id.
    story_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stories.id", ondelete="CASCADE"), nullable=False
    )
    raw_article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=False
    )
    #: correct | wrong | unsure. `unsure` is recorded rather than skipped: an article a
    #: human could not judge is a fact about the data, and dropping it would quietly
    #: bias the measurement towards the easy cases.
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    labelled_by: Mapped[str | None] = mapped_column(String(64))
    labelled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
