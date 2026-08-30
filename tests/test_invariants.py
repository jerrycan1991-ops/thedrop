"""Structural invariants that must never regress.

These are the rules from CLAUDE.md expressed as executable assertions. If one of these
fails, something load-bearing was changed without understanding why it was there.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import thedrop_config
from thedrop_config import (
    ARTICLE_TYPES,
    COMMERCIAL_TYPES,
    EDITORIAL_ARTICLE_TYPES,
    EDITORIAL_ARTICLE_TYPES_ORDERED,
    commercial_forbidden_sql,
)
from thedrop_database.enums import (
    PUBLISHABLE_RIGHTS,
    TRUSTED_FIELD_SOURCES,
    FieldSource,
    RightsStatus,
)
from thedrop_database.models import affiliate as affiliate_models

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestEditorialCommercialSeparation:
    def test_editorial_types_are_the_expected_four(self) -> None:
        assert {"NEWS", "ANALYSIS", "OPINION", "COMMENTARY"} == EDITORIAL_ARTICLE_TYPES

    def test_no_commercial_type_is_also_an_editorial_type(self) -> None:
        assert set(COMMERCIAL_TYPES) & set(ARTICLE_TYPES) == set()

    def test_database_check_constraint_excludes_editorial_types(self) -> None:
        # PostgreSQL cannot express a cross-table CHECK, so `articles` carries a
        # composite key on (id, article_type) and the affiliate tables hold a
        # composite FK plus this CHECK. That combination is what makes "no affiliate
        # links in news" a database guarantee rather than a policy.
        clause = affiliate_models._NOT_EDITORIAL
        for editorial_type in EDITORIAL_ARTICLE_TYPES:
            assert f"'{editorial_type}'" in clause

    def test_affiliate_article_table_carries_the_constraint(self) -> None:
        # Matched by suffix: SQLAlchemy's naming convention may or may not have been
        # applied to `.name` depending on when the constraint was attached.
        table = affiliate_models.AffiliateArticle.__table__
        names = [str(c.name) for c in table.constraints if c.name]
        assert any(name.endswith("not_editorial_type") for name in names), names


class TestSingleSourceOfTruth:
    """Phase 1 guarantees. See docs/DOMAIN_MODEL.md."""

    def test_article_types_have_exactly_one_definition(self) -> None:
        # The canonical file lives inside the Python package so it is present in both
        # an editable install and a built wheel. TypeScript imports the same file.
        definition = (
            Path(thedrop_config.__file__).parent / "article_types.json"
        )
        assert definition.is_file(), "canonical article-type definition is missing"

        data = json.loads(definition.read_text(encoding="utf-8"))
        assert set(data["editorial"]) == set(ARTICLE_TYPES)
        assert set(data["commercial"]) == set(COMMERCIAL_TYPES)

    def test_typescript_imports_the_same_definition(self) -> None:
        # If TypeScript ever stops importing the JSON, it has grown a second list.
        index_ts = REPO_ROOT / "packages" / "config" / "src" / "index.ts"
        source = index_ts.read_text(encoding="utf-8")
        assert "article_types.json" in source, "TypeScript no longer reads the canonical file"

    def test_typescript_has_no_hardcoded_category_list(self) -> None:
        # Categories are runtime rows. A `CATEGORIES = [...]` const in shared config
        # would be a second source of truth and is exactly what Phase 1 removed.
        source = (REPO_ROOT / "packages" / "config" / "src" / "index.ts").read_text(
            encoding="utf-8"
        )
        assert "export const CATEGORIES" not in source
        assert "export const CATEGORY_SLUGS" not in source

    def test_seed_categories_are_not_imported_by_application_code(self) -> None:
        # The seed may define initial rows, but nothing may import them at runtime.
        offenders = []
        for path in (REPO_ROOT / "services").rglob("*.py"):
            if "from thedrop_database.seed import" in path.read_text(encoding="utf-8"):
                offenders.append(str(path))
        assert offenders == [], f"seed data imported at runtime: {offenders}"

    def test_generated_check_constraint_matches_the_applied_schema(self) -> None:
        # Byte-identical to what revision bf45495a0cae applied. If this changes, the
        # database has drifted from the models and needs a migration -- silently
        # editing the generator would leave the constraint stale in production.
        assert (
            commercial_forbidden_sql()
            == "article_type NOT IN ('NEWS', 'ANALYSIS', 'OPINION', 'COMMENTARY')"
        )

    def test_editorial_order_is_stable(self) -> None:
        # Order is rendered into SQL, so it is part of the schema contract.
        assert EDITORIAL_ARTICLE_TYPES_ORDERED == ("NEWS", "ANALYSIS", "OPINION", "COMMENTARY")

    def test_every_forbidding_type_appears_in_the_constraint(self) -> None:
        sql = commercial_forbidden_sql()
        for name in EDITORIAL_ARTICLE_TYPES:
            assert f"'{name}'" in sql


class TestMediaRights:
    def test_unknown_and_prohibited_can_never_publish(self) -> None:
        assert RightsStatus.UNKNOWN not in PUBLISHABLE_RIGHTS
        assert RightsStatus.PROHIBITED not in PUBLISHABLE_RIGHTS

    def test_publishable_set_is_exactly_the_four_safe_statuses(self) -> None:
        assert {
            RightsStatus.ORIGINAL_AI,
            RightsStatus.LICENSED,
            RightsStatus.PUBLIC_DOMAIN,
            RightsStatus.VALIDATED_CC,
        } == PUBLISHABLE_RIGHTS


class TestProductDataProvenance:
    def test_only_api_and_admin_entry_are_trusted(self) -> None:
        # ADR-0009. Scraped structured data and OG tags are too unreliable to render
        # as a current price or a star rating.
        assert {FieldSource.API, FieldSource.ADMIN_OVERRIDE} == TRUSTED_FIELD_SOURCES
        assert FieldSource.STRUCTURED_DATA not in TRUSTED_FIELD_SOURCES
        assert FieldSource.OG_TAG not in TRUSTED_FIELD_SOURCES
        assert FieldSource.UNKNOWN not in TRUSTED_FIELD_SOURCES

    def test_price_renderability_requires_source_and_freshness(self) -> None:
        source = inspect.getsource(affiliate_models.AffiliateProduct.price_is_renderable)
        # The three conditions that keep a stale or untrusted price off the page.
        assert "price_source" in source
        assert "price_fetched_at" in source
        assert "max_age_hours" in source

    def test_rating_renderability_requires_the_api(self) -> None:
        source = inspect.getsource(affiliate_models.AffiliateProduct.rating_is_renderable)
        assert "FieldSource.API" in source

    def test_product_table_enforces_provenance_in_the_schema(self) -> None:
        table = affiliate_models.AffiliateProduct.__table__
        names = [str(c.name) for c in table.constraints if c.name]
        assert any(name.endswith("price_requires_provenance") for name in names), names
        assert any(name.endswith("rating_requires_provenance") for name in names), names


class TestLinkSafety:
    def test_broken_and_expired_links_are_never_shown(self) -> None:
        source = inspect.getsource(affiliate_models.AffiliateLink.is_safe_to_show.fget)  # type: ignore[attr-defined]
        assert "BROKEN" in source
        assert "EXPIRED" in source
