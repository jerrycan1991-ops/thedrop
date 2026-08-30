"""Enumerations shared across models.

Stored as native PostgreSQL ENUM types where the value set is stable. Sets that are
expected to grow (article types, job types) use TEXT with an application-level enum,
because adding a value to a PG enum requires a migration and a lock.
"""

from __future__ import annotations

from enum import StrEnum


class ArticleType(StrEnum):
    """Editorial types. These may NEVER carry an affiliate link (see CommercialType)."""

    NEWS = "NEWS"
    ANALYSIS = "ANALYSIS"
    OPINION = "OPINION"
    COMMENTARY = "COMMENTARY"
    BREAKING = "BREAKING"
    EXPLAINER = "EXPLAINER"
    LIVE = "LIVE"


#: The four types where commercial content is structurally forbidden.
EDITORIAL_ARTICLE_TYPES: frozenset[str] = frozenset(
    {ArticleType.NEWS, ArticleType.ANALYSIS, ArticleType.OPINION, ArticleType.COMMENTARY}
)


class CommercialType(StrEnum):
    """Affiliate article formats. Live under /picks, excluded from the news sitemap."""

    PRODUCT_REVIEW = "PRODUCT_REVIEW"
    BUYING_GUIDE = "BUYING_GUIDE"
    BEST_PRODUCTS_LIST = "BEST_PRODUCTS_LIST"
    PRODUCT_COMPARISON = "PRODUCT_COMPARISON"
    PRODUCT_ROUNDUP = "PRODUCT_ROUNDUP"
    GIFT_GUIDE = "GIFT_GUIDE"
    BEST_FOR_GUIDE = "BEST_FOR_GUIDE"
    TRENDING_PRODUCT = "TRENDING_PRODUCT"
    NEWS_PLUS_RECOMMENDATION = "NEWS_PLUS_RECOMMENDATION"
    HOW_TO = "HOW_TO"
    DEALS = "DEALS"


class ArticleStatus(StrEnum):
    DRAFT = "draft"
    QA = "qa"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    UPDATED = "updated"
    UNPUBLISHED = "unpublished"
    REJECTED = "rejected"


class RiskTier(StrEnum):
    """Drives how much evidence a claim needs. See PIPELINE.md §11."""

    STANDARD = "standard"
    ELEVATED = "elevated"
    HIGH = "high"


class CorrectionType(StrEnum):
    CORRECTION = "correction"
    CLARIFICATION = "clarification"
    UPDATE = "update"
    RETRACTION = "retraction"


class SourceType(StrEnum):
    WIRE = "wire"
    NATIONAL = "national"
    LOCAL = "local"
    GOVERNMENT = "government"
    ACADEMIC = "academic"
    TRADE = "trade"
    BLOG = "blog"
    AGGREGATOR = "aggregator"
    SOCIAL = "social"
    UNKNOWN = "unknown"


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class RightsStatus(StrEnum):
    ORIGINAL_AI = "ORIGINAL_AI"
    LICENSED = "LICENSED"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    VALIDATED_CC = "VALIDATED_CC"
    UNKNOWN = "UNKNOWN"
    PROHIBITED = "PROHIBITED"


#: Only these may appear on a published page. Enforced by the publish gate.
PUBLISHABLE_RIGHTS: frozenset[str] = frozenset(
    {
        RightsStatus.ORIGINAL_AI,
        RightsStatus.LICENSED,
        RightsStatus.PUBLIC_DOMAIN,
        RightsStatus.VALIDATED_CC,
    }
)


class MediaRole(StrEnum):
    HERO = "hero"
    SOCIAL = "social"
    VERTICAL = "vertical"
    BREAKING_CARD = "breaking_card"
    INLINE = "inline"
    THUMBNAIL = "thumbnail"
    VIDEO_POSTER = "video_poster"


class UsageStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerStatus(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class SubscriptionTier(StrEnum):
    FREE = "FREE"
    REGISTERED = "REGISTERED"
    PREMIUM = "PREMIUM"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    WORKER = "worker"
    AI = "ai"


class SubscriberStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    UNSUBSCRIBED = "unsubscribed"
    BOUNCED = "bounced"


# --------------------------------------------------------------------- affiliate


class AffiliateNetwork(StrEnum):
    AMAZON = "amazon"
    IMPACT = "impact"
    CJ = "cj"
    SHAREASALE = "shareasale"
    RAKUTEN = "rakuten"
    WALMART = "walmart"
    BESTBUY = "bestbuy"
    DIRECT = "direct"
    OTHER = "other"


class FieldSource(StrEnum):
    """Provenance for a single product attribute. See ADR-0009.

    Rendering rules key on this, not on presence. A price from ``OG_TAG`` is never
    shown as a current price; a rating from anything but ``API`` is never shown at all.
    """

    API = "api"
    STRUCTURED_DATA = "structured_data"
    OG_TAG = "og_tag"
    ADMIN_OVERRIDE = "admin_override"
    UNKNOWN = "unknown"


#: Only these two sources are trusted enough to render a price, rating or availability.
TRUSTED_FIELD_SOURCES: frozenset[str] = frozenset(
    {FieldSource.API, FieldSource.ADMIN_OVERRIDE}
)


class ProductStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    NEEDS_METADATA = "NEEDS_METADATA"
    LINK_ERROR = "LINK_ERROR"
    EXPIRED = "EXPIRED"


class AffiliateArticleStatus(StrEnum):
    DRAFT = "DRAFT"
    GENERATING = "GENERATING"
    QUALITY_CHECK = "QUALITY_CHECK"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class LinkHealth(StrEnum):
    OK = "ok"
    REDIRECTED = "redirected"
    BROKEN = "broken"
    EXPIRED = "expired"
    TIMEOUT = "timeout"
    UNCHECKED = "unchecked"


class CtaPlacement(StrEnum):
    AFTER_INTRO = "after_intro"
    AFTER_OVERVIEW = "after_overview"
    AFTER_FEATURES = "after_features"
    BEFORE_VERDICT = "before_verdict"
    ARTICLE_END = "article_end"
    PRODUCT_CARD = "product_card"


class PublishMode(StrEnum):
    DRAFT = "draft"
    AUTO = "auto"
    SCHEDULE = "schedule"
