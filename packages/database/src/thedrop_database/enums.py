"""Enumerations shared across models.

Stored as native PostgreSQL ENUM types where the value set is stable. Sets that are
expected to grow (article types, job types) use TEXT with an application-level enum,
because adding a value to a PG enum requires a migration and a lock.
"""

from __future__ import annotations

from enum import StrEnum

# NOTE: article types are deliberately NOT defined in this module.
#
# They are a closed, version-controlled set whose single definition is
# packages/config/src/thedrop_config/article_types.json, which TypeScript reads from
# the same file. Import them from `thedrop_config` directly:
#
#     from thedrop_config import DEFAULT_ARTICLE_TYPE, EDITORIAL_ARTICLE_TYPES
#
# Re-exporting them here would add an indirection layer whose only effect is to make
# the canonical location harder to find.
#
# Categories are the opposite case: runtime data owned by the `categories` table, with
# no hardcoded list in either language. See docs/DOMAIN_MODEL.md.


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
