"""Taxonomy and runtime settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from thedrop_database.base import Base, PrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from thedrop_database.models.content import Article

article_tags = Table(
    "article_tags",
    Base.metadata,
    Column(
        "article_id",
        BigInteger,
        ForeignKey("articles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tag_id", BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_article_tags_tag_id", "tag_id"),
)


class Category(Base, PrimaryKeyMixin, TimestampMixin):
    """A section of the site.

    Adding a category in production is a row plus a cache revalidation -- never a
    code change. ``accent_token`` names a CSS custom property; it is deliberately not
    a hex value, so theming stays in the token layer.
    """

    __tablename__ = "categories"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_commercial: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Commercial sections are excluded from the Google News sitemap.",
    )
    target_articles_per_day: Mapped[int | None] = mapped_column(Integer)
    seo_title: Mapped[str | None] = mapped_column(String(255))
    seo_description: Mapped[str | None] = mapped_column(Text)
    accent_token: Mapped[str] = mapped_column(String(64), default="--accent", nullable=False)

    articles: Mapped[list[Article]] = relationship(back_populates="category")

    def __repr__(self) -> str:
        return f"<Category {self.slug}>"


class Tag(Base, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tags"

    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_trending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    articles: Mapped[list[Article]] = relationship(secondary=article_tags, back_populates="tags")

    def __repr__(self) -> str:
        return f"<Tag {self.slug}>"


class Setting(Base, PrimaryKeyMixin, TimestampMixin):
    """Runtime configuration and kill switches.

    Read on each cycle so a change takes effect within ~60s without a restart. This is
    where ``publishing.enabled``, ``ai.enabled`` and ``ingestion.enabled`` live.

    ``is_protected`` marks settings the self-improvement framework may never modify
    (SECURITY.md §11). The flag is enforced in application code and asserted by a test.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<Setting {self.key}>"
