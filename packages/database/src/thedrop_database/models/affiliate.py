"""Affiliate content automation engine.

See docs/AFFILIATE_ENGINE.md and ADR-0009.

Two structural guarantees are enforced here rather than left to policy:

1. **No fabricated product data.** Every product attribute is stored in ``fields`` as
   ``{value, source, confidence, fetched_at}``. Rendering keys on provenance, not on
   presence: a price renders only from an official API or human entry and only inside
   a freshness window; a rating renders only from an official API. The flattened
   columns exist for querying and sorting -- never as the rendering source of truth.

2. **No commercial content inside editorial articles.** PostgreSQL cannot express a
   cross-table CHECK, so ``articles`` carries a composite unique key on
   ``(id, article_type)`` and the affiliate tables hold a composite foreign key to it
   plus a CHECK excluding the four editorial types. An affiliate CTA physically cannot
   attach to a NEWS, ANALYSIS, OPINION or COMMENTARY article.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import (
    AffiliateArticleStatus,
    AffiliateNetwork,
    CtaPlacement,
    FieldSource,
    LinkHealth,
    ProductStatus,
    PublishMode,
)

#: Reused by every table that attaches commercial content to an article.
_NOT_EDITORIAL = "article_type NOT IN ('NEWS', 'ANALYSIS', 'OPINION', 'COMMENTARY')"


class AffiliateMerchant(Base, PrimaryKeyMixin, TimestampMixin):
    """A retailer, reached through a network adapter.

    Nothing in the pipeline knows what Amazon is -- ``adapter_slug`` resolves to an
    ``AffiliateNetworkAdapter`` at runtime. ``allows_page_fetch`` is false where the
    merchant's terms forbid automated page access; the adapter then exposes only the
    API and manual metadata tiers.
    """

    __tablename__ = "affiliate_merchants"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    network: Mapped[str] = mapped_column(
        String(32), default=AffiliateNetwork.DIRECT, nullable=False, index=True
    )
    adapter_slug: Mapped[str] = mapped_column(String(64), default="generic", nullable=False)

    allows_page_fetch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image_rights: Mapped[str] = mapped_column(
        String(24),
        default="unknown",
        nullable=False,
        comment="api_licensed | prohibited | unknown. Product photography is used only "
        "when the network's API supplies it with explicit usage rights.",
    )

    logo_media_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_assets.id", ondelete="SET NULL")
    )
    commission_notes: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(
        String(128), comment="Secret-store key name. Never the secret."
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products: Mapped[list[AffiliateProduct]] = relationship(back_populates="merchant")

    def __repr__(self) -> str:
        return f"<AffiliateMerchant {self.slug}>"


class AffiliateProduct(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    __tablename__ = "affiliate_products"
    __table_args__ = (
        UniqueConstraint("merchant_id", "product_ref", name="uq_affiliate_products_merchant_ref"),
        Index("ix_affiliate_products_status", "status"),
        CheckConstraint(
            "price_amount IS NULL OR price_source IS NOT NULL",
            name="price_requires_provenance",
        ),
        CheckConstraint(
            "rating_value IS NULL OR rating_source IS NOT NULL",
            name="rating_requires_provenance",
        ),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_merchants.id", ondelete="CASCADE"), nullable=False
    )
    product_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Merchant SKU / ASIN / catalogue id."
    )

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(128))
    category_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    product_category: Mapped[str | None] = mapped_column(
        String(255), comment="The merchant's own taxonomy string."
    )
    description: Mapped[str | None] = mapped_column(Text)
    specifications: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    #: THE anti-fabrication mechanism. Maps attribute name ->
    #: {value, source, confidence, fetched_at}. The generator receives these, never a
    #: blob of prose to embellish. A field with value=None cannot be rendered at all.
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    price_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    price_currency: Mapped[str | None] = mapped_column(String(3))
    price_source: Mapped[str | None] = mapped_column(String(24))
    price_fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    rating_value: Mapped[float | None] = mapped_column(Numeric(3, 2))
    rating_count: Mapped[int | None] = mapped_column(Integer)
    rating_source: Mapped[str | None] = mapped_column(String(24))

    availability: Mapped[str | None] = mapped_column(String(32))
    availability_fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    primary_image_media_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_assets.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(
        String(24), default=ProductStatus.NEEDS_METADATA, nullable=False
    )
    missing_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        default=list,
        nullable=False,
        comment="Drives the admin 'Needs Metadata' queue. Naming what is missing is "
        "what keeps the one-click workflow honest instead of guessing.",
    )
    target_audience: Mapped[str | None] = mapped_column(String(128))

    added_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    last_refreshed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    merchant: Mapped[AffiliateMerchant] = relationship(back_populates="products")
    links: Mapped[list[AffiliateLink]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    def price_is_renderable(self, max_age_hours: int) -> bool:
        """A price may be shown as current only from a trusted source, and only fresh."""
        if self.price_amount is None or self.price_source is None:
            return False
        if self.price_source not in {FieldSource.API, FieldSource.ADMIN_OVERRIDE}:
            return False
        if self.price_fetched_at is None:
            return False
        age = dt.datetime.now(dt.UTC) - self.price_fetched_at
        return age <= dt.timedelta(hours=max_age_hours)

    def rating_is_renderable(self) -> bool:
        """Ratings come from a merchant API or they do not exist. No exceptions."""
        return self.rating_value is not None and self.rating_source == FieldSource.API

    def __repr__(self) -> str:
        return f"<AffiliateProduct {self.name!r} {self.status}>"


class AffiliateCampaign(Base, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "affiliate_campaigns"

    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    merchant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("affiliate_merchants.id", ondelete="SET NULL"), index=True
    )
    starts_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    clicks: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    conversions: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revenue: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)


class AffiliateLink(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """The link itself.

    Buttons point at ``/go/{public_id}`` (a first-party 302), never at the merchant
    directly. That makes clicks ours to measure and lets a link be disabled or swapped
    without editing published articles.
    """

    __tablename__ = "affiliate_links"
    __table_args__ = (Index("ix_affiliate_links_status", "status", "last_checked_at"),)

    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_products.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("affiliate_campaigns.id", ondelete="SET NULL"), index=True
    )

    original_url: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Exactly as pasted. Tracking parameters preserved."
    )
    destination_url: Mapped[str | None] = mapped_column(Text)
    destination_domain: Mapped[str | None] = mapped_column(String(255))
    network_tracking_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16), default=LinkHealth.UNCHECKED, nullable=False
    )
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    clicks: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    product: Mapped[AffiliateProduct] = relationship(back_populates="links")
    health_checks: Mapped[list[AffiliateLinkHealthCheck]] = relationship(
        back_populates="link", cascade="all, delete-orphan"
    )

    @property
    def is_safe_to_show(self) -> bool:
        """Never send a reader to a link we know is dead."""
        return self.is_active and self.status not in {LinkHealth.BROKEN, LinkHealth.EXPIRED}


class AffiliateDisclosure(Base, PrimaryKeyMixin, TimestampMixin):
    """Versioned disclosure text.

    Rendering is the article template's job, not the generator's, so a bad generation
    cannot omit it. An article records which version it published with.
    """

    __tablename__ = "affiliate_disclosures"
    __table_args__ = (UniqueConstraint("slug", "version", name="uq_affiliate_disclosures_slug_ver"),)

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    placement_default: Mapped[str] = mapped_column(String(16), default="both", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AffiliateCtaTemplate(Base, PrimaryKeyMixin, TimestampMixin):
    """Button text is chosen by rule from data availability -- never by the model.

    Fresh API price -> "Check Latest Price". Verified deal -> "See Today's Deal".
    Availability known -> "Check Availability". Otherwise "View Product on {merchant}".
    Deceptive wording is simply not in the vocabulary.
    """

    __tablename__ = "affiliate_cta_templates"

    name: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    button_text_template: Mapped[str] = mapped_column(String(96), nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    variant: Mapped[str] = mapped_column(String(32), default="primary", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AffiliateArticle(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """Commercial workflow state, joined to the standard ``articles`` row.

    The composite FK to ``(articles.id, articles.article_type)`` plus the CHECK below
    is what makes "no affiliate links in news" a database guarantee.
    """

    __tablename__ = "affiliate_articles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["article_id", "article_type"],
            ["articles.id", "articles.article_type"],
            name="fk_affiliate_articles_article",
            ondelete="CASCADE",
        ),
        CheckConstraint(_NOT_EDITORIAL, name="not_editorial_type"),
        UniqueConstraint("article_id", name="uq_affiliate_articles_article_id"),
        Index("ix_affiliate_articles_status", "status", "scheduled_for"),
    )

    article_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="Null until the article row is generated."
    )
    article_type: Mapped[str | None] = mapped_column(String(32))

    commercial_type: Mapped[str] = mapped_column(String(40), nullable=False)
    angle_rationale: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
        comment="Why this format was chosen. The angle is derived from evidence, not guessed.",
    )
    primary_keyword: Mapped[str | None] = mapped_column(String(160))
    target_audience: Mapped[str | None] = mapped_column(String(128))
    ranking_criteria: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="Required for any 'best' list. A ranking with no criteria fails QA."
    )

    status: Mapped[str] = mapped_column(
        String(24), default=AffiliateArticleStatus.DRAFT, nullable=False
    )
    publish_mode: Mapped[str] = mapped_column(
        String(16), default=PublishMode.DRAFT, nullable=False
    )
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    disclosure_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("affiliate_disclosures.id", ondelete="SET NULL")
    )
    qa_report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )

    products: Mapped[list[AffiliateArticleProduct]] = relationship(
        back_populates="affiliate_article", cascade="all, delete-orphan"
    )
    ctas: Mapped[list[AffiliateCTA]] = relationship(
        back_populates="affiliate_article", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<AffiliateArticle {self.commercial_type} {self.status}>"


class AffiliateArticleProduct(Base, PrimaryKeyMixin):
    """One row per product featured in an article. Roundups have many."""

    __tablename__ = "affiliate_article_products"
    __table_args__ = (
        UniqueConstraint(
            "affiliate_article_id", "product_id", name="uq_affiliate_article_products"
        ),
    )

    affiliate_article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_articles.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_products.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verdict_note: Mapped[str | None] = mapped_column(Text)

    affiliate_article: Mapped[AffiliateArticle] = relationship(back_populates="products")


class AffiliateCTA(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    __tablename__ = "affiliate_ctas"
    __table_args__ = (Index("ix_affiliate_ctas_article", "affiliate_article_id", "display_order"),)

    affiliate_article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_articles.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_products.id", ondelete="CASCADE"), nullable=False
    )
    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_links.id", ondelete="CASCADE"), nullable=False, index=True
    )

    placement: Mapped[str] = mapped_column(
        String(24), default=CtaPlacement.AFTER_INTRO, nullable=False
    )
    button_text: Mapped[str] = mapped_column(String(96), nullable=False)
    button_variant: Mapped[str] = mapped_column(String(32), default="primary", nullable=False)
    disclosure_mode: Mapped[str] = mapped_column(String(16), default="inline", nullable=False)

    is_visible: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Set false automatically when the link is unhealthy.",
    )
    impressions: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    affiliate_article: Mapped[AffiliateArticle] = relationship(back_populates="ctas")


class AffiliateClick(Base, PrimaryKeyMixin):
    """Append-only click log. Partitioned monthly; raw rows dropped after 90 days.

    First-party only. IPs are truncated before storage; there is no cross-site id.
    """

    __tablename__ = "affiliate_clicks"
    __table_args__ = (
        Index("ix_affiliate_clicks_link_time", "link_id", "clicked_at"),
        Index("ix_affiliate_clicks_time", "clicked_at"),
    )

    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_links.id", ondelete="CASCADE"), nullable=False
    )
    cta_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("affiliate_ctas.id", ondelete="SET NULL")
    )
    article_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    merchant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    clicked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    referrer_class: Mapped[str] = mapped_column(String(16), default="internal", nullable=False)
    device_class: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    ip_truncated: Mapped[str | None] = mapped_column(INET)
    session_hash: Mapped[str | None] = mapped_column(String(64))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AffiliateConversion(Base, PrimaryKeyMixin, TimestampMixin):
    """Populated only from what a network actually reports. Never estimated.

    Most networks offer postback or CSV export rather than a real-time API, so both
    import paths are supported. Where a network reports nothing, the dashboard shows
    "not reported" -- not a guess.
    """

    __tablename__ = "affiliate_conversions"

    link_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("affiliate_links.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("affiliate_campaigns.id", ondelete="SET NULL"), index=True
    )
    merchant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    external_order_ref: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    commission: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    import_source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class AffiliateLinkHealthCheck(Base, PrimaryKeyMixin):
    """Every check is recorded, so a flapping link is visible rather than silent."""

    __tablename__ = "affiliate_link_health_checks"
    __table_args__ = (Index("ix_affiliate_link_health_link_time", "link_id", "checked_at"),)

    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("affiliate_links.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(16), default=LinkHealth.OK, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    link: Mapped[AffiliateLink] = relationship(back_populates="health_checks")
