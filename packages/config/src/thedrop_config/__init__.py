"""Typed configuration for THE DROP.

Nothing in this package reads a hardcoded secret, model id, price or threshold.
Everything comes from the environment, with safe defaults that fail closed.

It also holds the canonical **article type** definition (``article_types.json``),
which TypeScript reads from the same file. Categories are deliberately NOT here:
they are runtime data owned by the ``categories`` table. See ``README.md``.
"""

from thedrop_config.article_types import (
    ALL_ARTICLE_TYPES,
    ARTICLE_TYPES,
    COMMERCIAL_TYPES,
    DEFAULT_ARTICLE_TYPE,
    EDITORIAL_ARTICLE_TYPES,
    EDITORIAL_ARTICLE_TYPES_ORDERED,
    commercial_forbidden_sql,
    is_editorial,
    is_known,
)
from thedrop_config.settings import (
    AffiliateSettings,
    AISettings,
    EditorialSettings,
    Environment,
    Settings,
    get_settings,
)

__all__ = [
    "ALL_ARTICLE_TYPES",
    "ARTICLE_TYPES",
    "COMMERCIAL_TYPES",
    "DEFAULT_ARTICLE_TYPE",
    "EDITORIAL_ARTICLE_TYPES",
    "EDITORIAL_ARTICLE_TYPES_ORDERED",
    "AISettings",
    "AffiliateSettings",
    "EditorialSettings",
    "Environment",
    "Settings",
    "commercial_forbidden_sql",
    "get_settings",
    "is_editorial",
    "is_known",
]
