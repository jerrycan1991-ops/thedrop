"""Articles, versions, attribution, corrections and media."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import (
    ArticleStatus,
    ArticleType,
    CorrectionType,
    MediaRole,
    RightsStatus,
    RiskTier,
    UsageStatus,
)
from thedrop_database.models.core import Tag, article_tags

if TYPE_CHECKING:
    from thedrop_database.models.core import Category


class Article(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """A published (or in-flight) piece of content.

    The public path is derived, never stored: ``/{category}/{yyyy}/{mm}/{dd}/{slug}``
    from ``first_published_at``. Storing it would let the two drift.
    """

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("category_id", "slug", name="uq_articles_category_slug"),
        # Composite key that lets affiliate tables enforce, at the database level,
        # that commercial content never attaches to an editorial article type.
        # Postgres cannot express a cross-table CHECK, so we carry the type into the
        # foreign key instead. See models/affiliate.py.
        UniqueConstraint("id", "article_type", name="uq_articles_id_article_type"),
        Index("ix_articles_status_published_at", "status", "published_at"),
        Index(
            "ix_articles_live",
            "published_at",
            postgresql_where=text("status = 'published'"),
        ),
        Index("ix_articles_category_published", "category_id", "published_at"),
        CheckConstraint(
            "editorial_confidence IS NULL OR (editorial_confidence BETWEEN 0 AND 100)",
            name="editorial_confidence_range",
        ),
    )

    story_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    article_type: Mapped[str] = mapped_column(
        String(32), default=ArticleType.NEWS, nullable=False, index=True
    )

    headline: Mapped[str] = mapped_column(String(300), nullable=False)
    alternate_headlines: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False, comment="Candidates with their per-axis scores."
    )
    dek: Mapped[str] = mapped_column(Text, nullable=False, default="")

    body_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
        comment="Block list, so ad slots and CTAs are placed between blocks by rule "
        "and the renderer never needs dangerouslySetInnerHTML on model output.",
    )
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    key_facts: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    byline: Mapped[str] = mapped_column(String(128), default="The Drop Newsroom", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reading_time_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(24), default=ArticleStatus.DRAFT, nullable=False, index=True
    )
    editorial_confidence: Mapped[int | None] = mapped_column(SmallInteger)
    qa_report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    risk_tier: Mapped[str] = mapped_column(
        String(16), default=RiskTier.STANDARD, nullable=False
    )

    first_published_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Immutable once set. Drives the URL date path."
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_public: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    seo_title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    meta_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    og_title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    og_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    structured_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    noindex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # `articles` and `media_assets` reference each other: an article names its hero
    # asset, and an asset names the article it belongs to. That cycle has no valid
    # CREATE TABLE ordering, and without use_alter Alembic silently gives up sorting
    # and emits the tables alphabetically -- producing a migration that fails on the
    # first foreign key it hits.
    #
    # use_alter defers THIS side to an ALTER TABLE after both tables exist, which
    # breaks the cycle. The name is explicit because ALTER-added constraints cannot
    # use the metadata naming convention.
    hero_media_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "media_assets.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_articles_hero_media_id_media_assets",
        ),
        index=True,
    )

    is_sponsored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disclosure_text: Mapped[str | None] = mapped_column(Text)

    view_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    share_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    category: Mapped[Category] = relationship(back_populates="articles")
    tags: Mapped[list[Tag]] = relationship(secondary=article_tags, back_populates="articles")
    hero_media: Mapped[MediaAsset | None] = relationship(foreign_keys=[hero_media_id])
    versions: Mapped[list[ArticleVersion]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    source_refs: Mapped[list[ArticleSourceRef]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    corrections: Mapped[list[Correction]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    @property
    def path(self) -> str:
        """Public URL path. Unpublished articles have no public path."""
        if self.first_published_at is None:
            return f"/preview/{self.public_id}"
        d = self.first_published_at
        return f"/{self.category.slug}/{d:%Y/%m/%d}/{self.slug}"

    def __repr__(self) -> str:
        return f"<Article {self.status} {self.slug!r}>"


class ArticleVersion(Base, PrimaryKeyMixin):
    """Immutable snapshot on every material change. Never updated, never deleted."""

    __tablename__ = "article_versions"
    __table_args__ = (
        UniqueConstraint("article_id", "version", name="uq_article_versions_article_version"),
    )

    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    headline: Mapped[str] = mapped_column(String(300), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    changed_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    article: Mapped[Article] = relationship(back_populates="versions")


class ArticleSourceRef(Base, PrimaryKeyMixin):
    """Reader-visible attribution.

    Every reference must resolve to something we actually ingested. The publish gate
    re-checks this, which is how invented sources are caught (SECURITY.md §6.3).
    """

    __tablename__ = "article_source_refs"
    __table_args__ = (Index("ix_article_source_refs_article", "article_id", "display_order"),)

    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    raw_article_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str] = mapped_column(String(255), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(24), default="reporting", nullable=False)
    accessed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    article: Mapped[Article] = relationship(back_populates="source_refs")


class Correction(Base, PrimaryKeyMixin, TimestampMixin):
    """Public corrections render on the article and on /corrections. Permanent."""

    __tablename__ = "corrections"

    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    correction_type: Mapped[str] = mapped_column(
        String(24), default=CorrectionType.CORRECTION, nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    field_changed: Mapped[str | None] = mapped_column(String(64))
    previous_value: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    issued_by: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    article: Mapped[Article] = relationship(back_populates="corrections")


class MediaAsset(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """An image or graphic.

    ``rights_status`` gates publication: only ORIGINAL_AI, LICENSED, PUBLIC_DOMAIN and
    VALIDATED_CC may go live. ``alt_text`` is required before publishing. Generated
    imagery is always labeled and never presented as documentary photography.
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_media_assets_article_role", "article_id", "asset_role"),
        CheckConstraint("width > 0 AND height > 0", name="positive_dimensions"),
    )

    article_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="SET NULL")
    )
    story_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    asset_role: Mapped[str] = mapped_column(String(24), default=MediaRole.HERO, nullable=False)
    storage_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Path or object key -- never a fully-qualified URL."
    )
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    blurhash: Mapped[str | None] = mapped_column(String(64))

    alt_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    caption: Mapped[str | None] = mapped_column(Text)
    credit: Mapped[str | None] = mapped_column(Text)

    rights_status: Mapped[str] = mapped_column(
        String(24), default=RightsStatus.UNKNOWN, nullable=False, index=True
    )
    license_ref: Mapped[str | None] = mapped_column(Text)
    attribution_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_disclosure_text: Mapped[str | None] = mapped_column(Text)
    generator_model: Mapped[str | None] = mapped_column(String(128))
    prompt_text: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    generation_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    safety_report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    usage_status: Mapped[str] = mapped_column(
        String(16), default=UsageStatus.DRAFT, nullable=False, index=True
    )
    cost: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    derivatives: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False, comment="AVIF/WebP/JPEG variants, generated on ingest."
    )
    source_urls: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), comment="Reference only, for provenance. Third-party media is never rehosted."
    )

    def __repr__(self) -> str:
        return f"<MediaAsset {self.asset_role} {self.rights_status}>"
