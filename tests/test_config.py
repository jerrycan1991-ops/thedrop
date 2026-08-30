"""Configuration invariants.

These tests exist because the publishing gates are the safety mechanism. A settings
change that silently inverts a threshold would be invisible in review and catastrophic
in production.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from thedrop_config.settings import (
    AffiliateSettings,
    AISettings,
    EditorialSettings,
    Environment,
    Settings,
)


class TestEditorialGates:
    def test_default_gates_are_ordered(self) -> None:
        gates = EditorialSettings()
        assert gates.gate_auto_publish_min >= gates.gate_verify_then_publish_min
        assert gates.gate_verify_then_publish_min >= gates.gate_second_review_min

    def test_verify_gate_cannot_exceed_auto_gate(self) -> None:
        # An inverted pair would make the "extra verification" band publish MORE
        # freely than the high-confidence band.
        with pytest.raises(ValidationError):
            EditorialSettings(
                GATE_AUTO_PUBLISH_MIN=80,
                GATE_VERIFY_THEN_PUBLISH_MIN=90,
            )

    def test_second_review_gate_cannot_exceed_verify_gate(self) -> None:
        with pytest.raises(ValidationError):
            EditorialSettings(
                GATE_VERIFY_THEN_PUBLISH_MIN=70,
                GATE_SECOND_REVIEW_MIN=85,
            )

    def test_publishing_is_off_by_default(self) -> None:
        # A fresh deployment must not publish anything until told to.
        assert EditorialSettings().publishing_enabled is False

    def test_daily_target_range_must_be_sane(self) -> None:
        with pytest.raises(ValidationError):
            EditorialSettings(DAILY_ARTICLE_TARGET_MIN=30, DAILY_ARTICLE_TARGET_MAX=20)

    def test_scores_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            EditorialSettings(GATE_AUTO_PUBLISH_MIN=101)


class TestAISettings:
    def test_ai_is_off_by_default(self) -> None:
        assert AISettings().enabled is False

    def test_ai_is_not_usable_without_a_key(self) -> None:
        # Enabled but uncredentialed must not count as usable, or jobs get scheduled
        # that can only fail.
        assert AISettings(AI_ENABLED=True, ANTHROPIC_API_KEY="").is_usable is False

    def test_ai_is_usable_when_enabled_and_credentialed(self) -> None:
        assert AISettings(AI_ENABLED=True, ANTHROPIC_API_KEY="sk-test").is_usable is True

    def test_embedding_dimensions_match_the_shared_vector_space(self) -> None:
        # ADR-0005: one 384-dimension space. Changing this silently corrupts every
        # similarity comparison in the system.
        assert AISettings().embedding_dimensions == 384


class TestAffiliateSettings:
    def test_affiliate_is_off_by_default(self) -> None:
        assert AffiliateSettings().enabled is False

    def test_price_freshness_window_is_positive(self) -> None:
        with pytest.raises(ValidationError):
            AffiliateSettings(AFFILIATE_PRICE_MAX_AGE_HOURS=0)

    def test_thin_content_floors_are_enforced(self) -> None:
        settings = AffiliateSettings()
        assert settings.min_word_count >= 700
        assert settings.min_extracted_facts >= 5


class TestProductionSafety:
    def test_placeholder_session_secret_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                ENVIRONMENT=Environment.PRODUCTION,
                SESSION_SECRET="CHANGE_ME_32_BYTES_MINIMUM_placeholder",
            )

    def test_short_session_secret_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError):
            Settings(ENVIRONMENT=Environment.PRODUCTION, SESSION_SECRET="short")

    def test_development_tolerates_weak_secrets(self) -> None:
        # Local development must not require secret management to run.
        settings = Settings(ENVIRONMENT=Environment.DEVELOPMENT, SESSION_SECRET="dev")
        assert settings.is_production is False
