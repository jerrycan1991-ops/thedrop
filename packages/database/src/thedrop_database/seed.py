"""Idempotent seed data.

Safe to run repeatedly: every insert is guarded by an existence check. Creates the
category taxonomy, default settings, roles, an initial admin, and the affiliate
disclosure and CTA templates.

The admin password comes from ``ADMIN_INITIAL_PASSWORD`` and is never defaulted to a
known value. Run with:  ``pnpm db:seed``
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import select
from thedrop_config import get_settings

from thedrop_database import session_scope
from thedrop_database.models import (
    AdPlacement,
    AffiliateCtaTemplate,
    AffiliateDisclosure,
    Category,
    Role,
    Setting,
    User,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")

# INITIAL DATA ONLY -- not a runtime source of truth.
#
# These rows bootstrap an empty database. Once seeded, the `categories` TABLE is
# authoritative: the site reads categories from it, and adding a category is a row,
# not an edit here. This list is intentionally never imported by application code, and
# the seed is idempotent, so editing it does not retroactively change existing rows.
CATEGORIES = [
    ("trending", "Trending", "What the country is paying attention to right now.", 1),
    ("politics", "Politics", "US politics, policy and power.", 2),
    ("entertainment", "Entertainment", "Film, television, music and celebrity.", 3),
    ("sports", "Sports", "Leagues, games and the stories around them.", 4),
    ("business", "Business", "Markets, companies and the economy.", 5),
    ("technology", "Technology", "Products, platforms and the people building them.", 6),
    ("world", "World", "International news that matters to a US audience.", 7),
]

ROLES = [
    ("admin", "Administrator", "Full access, including settings and secrets rotation."),
    ("editor", "Editor", "Content, publishing and corrections."),
    ("analyst", "Analyst", "Read access plus analytics."),
    ("viewer", "Viewer", "Read-only."),
]

# ``is_protected`` marks the verification, security and audit controls. The
# self-improvement framework may never modify these (SECURITY.md §11).
SETTINGS: list[tuple[str, dict, str, bool]] = [
    ("publishing.enabled", {"value": False}, "Master publish switch.", True),
    ("ai.enabled", {"value": False}, "Emergency AI kill switch.", True),
    ("ingestion.enabled", {"value": False}, "Master ingestion switch.", False),
    ("ads.enabled", {"value": False}, "Master ad switch.", False),
    ("affiliate.enabled", {"value": False}, "Master affiliate switch.", False),
    (
        "gates.thresholds",
        {"auto_publish": 95, "verify_then_publish": 85, "second_review": 70},
        "Publishing confidence gates. Never lowered automatically.",
        True,
    ),
    (
        "verification.high_risk_requires_corroboration",
        {"value": True},
        "High-risk load-bearing claims need two independent sources or an authority.",
        True,
    ),
    (
        "media.publishable_rights",
        {"value": ["ORIGINAL_AI", "LICENSED", "PUBLIC_DOMAIN", "VALIDATED_CC"]},
        "Rights statuses permitted to auto-publish.",
        True,
    ),
    (
        "affiliate.price_max_age_hours",
        {"value": 24},
        "A price older than this is never rendered as current.",
        True,
    ),
    (
        "daily_target",
        {"min": 20, "max": 30},
        "Scheduling preference only. Never a reason to publish.",
        False,
    ),
]

DEFAULT_DISCLOSURE = (
    "Disclosure: This article contains affiliate links. If you buy through them, "
    "The Drop may earn a commission at no additional cost to you."
)

# Button text is chosen by rule from what data we actually have -- never by the model,
# and never with deceptive urgency.
CTA_TEMPLATES = [
    ("fresh_price", "Check Latest Price", {"requires": ["fresh_api_price"]}, 100),
    ("verified_deal", "See Today's Deal", {"requires": ["verified_deal"]}, 90),
    ("availability", "Check Availability", {"requires": ["availability_known"]}, 80),
    ("generic", "View Product on {merchant}", {"requires": []}, 10),
]

AD_SLOTS = [
    "header",
    "after_intro",
    "mid_article",
    "sidebar",
    "article_end",
    "home_module",
]


def seed() -> int:
    settings = get_settings()
    created: dict[str, int] = {}

    with session_scope() as db:
        for slug, name, description, order in CATEGORIES:
            if db.scalar(select(Category).where(Category.slug == slug)) is None:
                db.add(
                    Category(
                        slug=slug,
                        name=name,
                        description=description,
                        sort_order=order,
                        # Matches the --cat-* custom properties in globals.css.
                        accent_token=f"--cat-{slug}",
                        is_active=True,
                    )
                )
                created["categories"] = created.get("categories", 0) + 1

        # Commercial content lives in its own section, excluded from the Google News
        # sitemap, so scaled affiliate output cannot damage news standing.
        if db.scalar(select(Category).where(Category.slug == "picks")) is None:
            db.add(
                Category(
                    slug="picks",
                    name="Picks",
                    description="Product guides and recommendations. Contains affiliate links.",
                    sort_order=90,
                    accent_token="--cat-picks",
                    is_active=True,
                    is_commercial=True,
                )
            )
            created["categories"] = created.get("categories", 0) + 1

        for slug, name, description in ROLES:
            if db.scalar(select(Role).where(Role.slug == slug)) is None:
                db.add(Role(slug=slug, name=name, description=description))
                created["roles"] = created.get("roles", 0) + 1

        for key, value, description, protected in SETTINGS:
            if db.scalar(select(Setting).where(Setting.key == key)) is None:
                db.add(
                    Setting(key=key, value=value, description=description, is_protected=protected)
                )
                created["settings"] = created.get("settings", 0) + 1

        disclosure_exists = db.scalar(
            select(AffiliateDisclosure).where(AffiliateDisclosure.slug == "default")
        )
        if disclosure_exists is None:
            db.add(
                AffiliateDisclosure(
                    slug="default",
                    version=1,
                    text_body=DEFAULT_DISCLOSURE,
                    placement_default="both",
                )
            )
            created["disclosures"] = 1

        for name, template, condition, priority in CTA_TEMPLATES:
            template_exists = db.scalar(
                select(AffiliateCtaTemplate).where(AffiliateCtaTemplate.name == name)
            )
            if template_exists is None:
                db.add(
                    AffiliateCtaTemplate(
                        name=name,
                        button_text_template=template,
                        condition=condition,
                        priority=priority,
                    )
                )
                created["cta_templates"] = created.get("cta_templates", 0) + 1

        for slot in AD_SLOTS:
            if db.scalar(select(AdPlacement).where(AdPlacement.slot_key == slot)) is None:
                # Inactive by default, and blocked on high-risk stories from the start.
                db.add(
                    AdPlacement(
                        slot_key=slot,
                        provider="none",
                        is_active=False,
                        excluded_risk_tiers=["high"],
                    )
                )
                created["ad_placements"] = created.get("ad_placements", 0) + 1

    # Admin user needs the roles committed first, so it runs in a second transaction.
    #
    # Read from settings, not os.environ: pydantic-settings loads .env into the Settings
    # object, it does NOT export those values into the process environment. Reading
    # os.environ here silently skipped admin creation whenever .env was the only source.
    password = settings.admin_initial_password
    email = settings.admin_email.lower()

    if not email or not password:
        logger.warning(
            "ADMIN_EMAIL / ADMIN_INITIAL_PASSWORD not set - skipping admin user creation"
        )
    else:
        # Imported here so the seed script does not require the API package unless an
        # admin is actually being created.
        from argon2 import PasswordHasher

        with session_scope() as db:
            if db.scalar(select(User).where(User.email == email)) is None:
                admin_role = db.scalar(select(Role).where(Role.slug == "admin"))
                user = User(
                    email=email,
                    password_hash=PasswordHasher(
                        memory_cost=65536, time_cost=3, parallelism=4
                    ).hash(password),
                    display_name="Administrator",
                    is_active=True,
                )
                if admin_role is not None:
                    user.roles.append(admin_role)
                db.add(user)
                created["admin_user"] = 1
                logger.info("created admin user %s", email)

    summary = ", ".join(f"{v} {k}" for k, v in sorted(created.items())) or "nothing new"
    logger.info("seed complete (%s): %s", settings.environment.value, summary)
    return 0


if __name__ == "__main__":
    sys.exit(seed())
