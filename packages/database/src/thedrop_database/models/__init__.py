"""All ORM models.

Importing this package registers every model on ``Base.metadata``, which is what
Alembic autogenerate walks. A model that is not imported here is invisible to
migrations -- so new model modules must be added to this file.
"""

from thedrop_database.models.affiliate import (
    AffiliateArticle,
    AffiliateArticleProduct,
    AffiliateCampaign,
    AffiliateClick,
    AffiliateConversion,
    AffiliateCTA,
    AffiliateCtaTemplate,
    AffiliateDisclosure,
    AffiliateLink,
    AffiliateLinkHealthCheck,
    AffiliateMerchant,
    AffiliateProduct,
)
from thedrop_database.models.auth import AuditLog, Role, User, user_roles
from thedrop_database.models.content import (
    Article,
    ArticleSourceRef,
    ArticleVersion,
    Correction,
    MediaAsset,
)
from thedrop_database.models.core import Category, Setting, Tag, article_tags
from thedrop_database.models.growth import AdPlacement, NewsletterSubscriber
from thedrop_database.models.ingestion import Provider, Source
from thedrop_database.models.ops import Job, WorkerNode

__all__ = [
    "AdPlacement",
    "AffiliateArticle",
    "AffiliateArticleProduct",
    "AffiliateCTA",
    "AffiliateCampaign",
    "AffiliateClick",
    "AffiliateConversion",
    "AffiliateCtaTemplate",
    "AffiliateDisclosure",
    "AffiliateLink",
    "AffiliateLinkHealthCheck",
    "AffiliateMerchant",
    "AffiliateProduct",
    "Article",
    "ArticleSourceRef",
    "ArticleVersion",
    "AuditLog",
    "Category",
    "Correction",
    "Job",
    "MediaAsset",
    "NewsletterSubscriber",
    "Provider",
    "Role",
    "Setting",
    "Source",
    "Tag",
    "User",
    "WorkerNode",
    "article_tags",
    "user_roles",
]
