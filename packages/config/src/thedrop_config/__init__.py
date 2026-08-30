"""Typed configuration for THE DROP.

Nothing in this package reads a hardcoded secret, model id, price or threshold.
Everything comes from the environment, with safe defaults that fail closed.
"""

from thedrop_config.settings import (
    AffiliateSettings,
    AISettings,
    EditorialSettings,
    Environment,
    Settings,
    get_settings,
)

__all__ = [
    "AISettings",
    "AffiliateSettings",
    "EditorialSettings",
    "Environment",
    "Settings",
    "get_settings",
]
