"""Application settings.

Loaded once, cached, and validated at import time by every service. A missing or
malformed required value must fail loudly at startup, never surface as a runtime
``None`` three layers deep.

Defaults are deliberately conservative: publishing off, AI off, ads off, affiliate
off. A fresh deployment does nothing until it is explicitly told to.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[4]

# Values that must never survive into a non-development environment.
_PLACEHOLDER_MARKERS = ("CHANGE_ME", "changeme", "placeholder", "example")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class BudgetAction(StrEnum):
    WARN = "warn"
    THROTTLE = "throttle"
    HALT = "halt"


class EditorialSettings(BaseSettings):
    """Publishing gates and scoring thresholds.

    These are configuration, not constants, because they are tuned. They are also
    PROTECTED: the self-improvement framework may never lower them (SECURITY.md §11).
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    publishing_enabled: bool = Field(default=False, alias="PUBLISHING_ENABLED")

    gate_auto_publish_min: int = Field(default=95, ge=0, le=100, alias="GATE_AUTO_PUBLISH_MIN")
    gate_verify_then_publish_min: int = Field(
        default=85, ge=0, le=100, alias="GATE_VERIFY_THEN_PUBLISH_MIN"
    )
    gate_second_review_min: int = Field(default=70, ge=0, le=100, alias="GATE_SECOND_REVIEW_MIN")

    us_relevance_min: int = Field(default=35, ge=0, le=100, alias="US_RELEVANCE_MIN")
    cluster_join_threshold: float = Field(
        default=0.82, ge=0.0, le=1.0, alias="CLUSTER_JOIN_THRESHOLD"
    )
    viral_half_life_hours: float = Field(default=8.0, gt=0, alias="VIRAL_HALF_LIFE_HOURS")

    daily_article_target_min: int = Field(default=20, ge=0, alias="DAILY_ARTICLE_TARGET_MIN")
    daily_article_target_max: int = Field(default=30, ge=0, alias="DAILY_ARTICLE_TARGET_MAX")

    @field_validator("gate_verify_then_publish_min")
    @classmethod
    def _verify_below_auto(cls, v: int, info: ValidationInfo) -> int:
        auto = info.data.get("gate_auto_publish_min")
        if auto is not None and v > auto:
            msg = "GATE_VERIFY_THEN_PUBLISH_MIN must not exceed GATE_AUTO_PUBLISH_MIN"
            raise ValueError(msg)
        return v

    @field_validator("gate_second_review_min")
    @classmethod
    def _review_below_verify(cls, v: int, info: ValidationInfo) -> int:
        verify = info.data.get("gate_verify_then_publish_min")
        if verify is not None and v > verify:
            msg = "GATE_SECOND_REVIEW_MIN must not exceed GATE_VERIFY_THEN_PUBLISH_MIN"
            raise ValueError(msg)
        return v

    @field_validator("daily_article_target_max")
    @classmethod
    def _max_above_min(cls, v: int, info: ValidationInfo) -> int:
        low = info.data.get("daily_article_target_min")
        if low is not None and v < low:
            msg = "DAILY_ARTICLE_TARGET_MAX must not be below DAILY_ARTICLE_TARGET_MIN"
            raise ValueError(msg)
        return v


class AISettings(BaseSettings):
    """Model routing and budgets.

    Model ids are configuration. Per-token prices are NOT stored here -- they live in
    the ``model_pricing`` table and must be filled from the provider's current pricing
    before cost gates are enabled. An invented price is worse than no price.
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    enabled: bool = Field(default=False, alias="AI_ENABLED")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    tier_cheap: str = Field(default="claude-haiku-4-5-20251001", alias="MODEL_TIER_CHEAP")
    tier_standard: str = Field(default="claude-sonnet-5", alias="MODEL_TIER_STANDARD")
    tier_critical: str = Field(default="claude-opus-5", alias="MODEL_TIER_CRITICAL")

    ollama_base_url: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_BASE_URL")
    embedding_model: str = Field(default="BAAI/bge-small-en-v1.5", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=384, alias="EMBEDDING_DIMENSIONS")
    #: Articles per embedding job. Bounded by what fits in one GPU forward pass and by
    #: the job payload it has to travel in, not by how many are waiting.
    embedding_batch_size: int = Field(default=32, ge=1, le=128, alias="EMBEDDING_BATCH_SIZE")
    #: Batches queued per beat tick. Bounds a cold start: a large backlog queued all at
    #: once would sit in front of everything else, for a desktop that may be offline.
    embedding_max_batches_per_tick: int = Field(
        default=8, ge=1, le=64, alias="EMBEDDING_MAX_BATCHES_PER_TICK"
    )
    #: NER for the clustering guard (PIPELINE.md 6). Smaller batches than embedding:
    #: each item carries more text and a token classifier costs more per article.
    entity_model: str = Field(default="dslim/bert-base-NER", alias="ENTITY_MODEL")
    entity_batch_size: int = Field(default=16, ge=1, le=64, alias="ENTITY_BATCH_SIZE")
    entity_max_batches_per_tick: int = Field(
        default=8, ge=1, le=64, alias="ENTITY_MAX_BATCHES_PER_TICK"
    )
    #: An entity in more than this share of the corpus stops discriminating and may not
    #: license a cluster join. Measured, not guessed: "United States" appeared in 18% of
    #: the first 152 articles, which would have let any two US stories merge.
    entity_guard_max_doc_fraction: float = Field(
        default=0.10, gt=0, le=1, alias="ENTITY_GUARD_MAX_DOC_FRACTION"
    )
    #: Floor below which the fraction is ignored. A young corpus otherwise excludes
    #: everything -- at 20 articles a 10% ceiling rejects anything seen twice.
    entity_guard_min_doc_floor: int = Field(default=5, ge=1, alias="ENTITY_GUARD_MIN_DOC_FLOOR")

    #: Cosine similarity at or above which an article may join a story -- PIPELINE.md 6.
    #: The entity guard applies independently; both are required and neither substitutes
    #: for the other.
    cluster_join_threshold: float = Field(default=0.82, gt=0, le=1, alias="CLUSTER_JOIN_THRESHOLD")
    #: How far back a story stays open to new members. A story nobody has written about
    #: in two days is over; a later article on the subject is a follow-up, not a member.
    cluster_window_hours: int = Field(default=48, ge=1, alias="CLUSTER_WINDOW_HOURS")
    #: Nearest centroids considered per article. If the right cluster is not in the top
    #: ten by cosine distance, it is not the right cluster.
    cluster_candidate_limit: int = Field(default=10, ge=1, le=100, alias="CLUSTER_CANDIDATE_LIMIT")
    #: Articles clustered per tick. Bounds a cold start on a large backlog.
    cluster_max_per_tick: int = Field(default=200, ge=1, le=5000, alias="CLUSTER_MAX_PER_TICK")
    #: Higher than the join threshold on purpose. Adding an article risks one wrong
    #: member; merging two stories asserts everything already in both is one event.
    cluster_merge_threshold: float = Field(
        default=0.90, gt=0, le=1, alias="CLUSTER_MERGE_THRESHOLD"
    )
    #: Merges per pass. A cap so a threshold set too low is a bounded mistake that shows
    #: up in a log rather than a cascade that flattens a day of stories.
    cluster_max_merges_per_pass: int = Field(
        default=50, ge=1, le=1000, alias="CLUSTER_MAX_MERGES_PER_PASS"
    )

    daily_budget_usd: float = Field(default=0.0, ge=0, alias="DAILY_AI_BUDGET_USD")
    monthly_budget_usd: float = Field(default=0.0, ge=0, alias="MONTHLY_AI_BUDGET_USD")
    budget_action_on_breach: BudgetAction = Field(
        default=BudgetAction.HALT, alias="BUDGET_ACTION_ON_BREACH"
    )

    #: PIPELINE.md 10 specifies Claude Haiku. "ollama" is a deliberate deviation while
    #: local-model quality on this task is still being measured -- see ADR-0020. Kept
    #: switchable, not hardcoded, because that measurement may conclude either way.
    claim_extract_provider: str = Field(default="ollama", alias="CLAIM_EXTRACT_PROVIDER")
    #: 12GB-VRAM-sized. The 14B tier of the same family left under 500MB of headroom on
    #: a single moderate-length story and would very likely spill to CPU on a real,
    #: multi-article evidence packet -- see ADR-0020's benchmark.
    claim_extract_ollama_model: str = Field(
        default="qwen2.5:7b", alias="CLAIM_EXTRACT_OLLAMA_MODEL"
    )
    claim_extract_anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="CLAIM_EXTRACT_ANTHROPIC_MODEL"
    )
    #: Stories dispatched per beat tick. One model call per story (its whole evidence
    #: packet at once, not per article) -- see entity_max_batches_per_tick for why a
    #: cap exists at all: bounding a cold-start backlog.
    claim_extract_max_stories_per_tick: int = Field(
        default=20, ge=1, le=200, alias="CLAIM_EXTRACT_MAX_STORIES_PER_TICK"
    )

    #: Same knob as claim_extract_max_stories_per_tick, for the contradiction-check
    #: stage that follows it (ADR-0020, ADR-0023). Kept separate rather than shared: the
    #: two stages dispatch on different gates and there is no reason a tuning change to
    #: one should silently retune the other.
    contradiction_check_max_stories_per_tick: int = Field(
        default=20, ge=1, le=200, alias="CONTRADICTION_CHECK_MAX_STORIES_PER_TICK"
    )

    @property
    def is_usable(self) -> bool:
        """AI work may only be scheduled when enabled AND credentialed."""
        return self.enabled and bool(self.anthropic_api_key)

    @property
    def claim_extraction_is_usable(self) -> bool:
        """Claim extraction has its own gate: the ollama path needs no Anthropic key,
        so it must not be blocked by ``is_usable``'s anthropic-only check."""
        if not self.enabled:
            return False
        if self.claim_extract_provider == "ollama":
            return True
        return bool(self.anthropic_api_key)


class AffiliateSettings(BaseSettings):
    """Affiliate engine (Phase 5B). See docs/AFFILIATE_ENGINE.md and ADR-0009."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    enabled: bool = Field(default=False, alias="AFFILIATE_ENABLED")

    # A price older than this is never rendered as current. The CTA falls back to
    # "Check Latest Price" instead of showing a stale number.
    price_max_age_hours: int = Field(default=24, gt=0, alias="AFFILIATE_PRICE_MAX_AGE_HOURS")
    link_check_interval_hours: int = Field(
        default=6, gt=0, alias="AFFILIATE_LINK_CHECK_INTERVAL_HOURS"
    )

    # Thin-content floors. A product whose metadata cannot support these does not get
    # a standalone article -- it is folded into a roundup, or nothing is written.
    min_word_count: int = Field(default=700, gt=0)
    min_extracted_facts: int = Field(default=5, gt=0)
    max_keyword_density: float = Field(default=0.02, gt=0, le=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Field(default=Environment.DEVELOPMENT, alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    site_url: str = Field(default="http://localhost:3100", alias="SITE_URL")
    site_name: str = Field(default="The Drop", alias="SITE_NAME")

    database_url: PostgresDsn = Field(
        default="postgresql+psycopg://thedrop:thedrop@127.0.0.1:5432/thedrop",  # type: ignore[assignment]
        alias="DATABASE_URL",
    )
    redis_url: RedisDsn = Field(
        default="redis://127.0.0.1:6379/0",  # type: ignore[assignment]
        alias="REDIS_URL",
    )
    celery_broker_url: str = Field(default="redis://127.0.0.1:6379/1", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(
        default="redis://127.0.0.1:6379/2", alias="CELERY_RESULT_BACKEND"
    )

    session_secret: str = Field(
        default_factory=lambda: secrets.token_urlsafe(48), alias="SESSION_SECRET"
    )
    session_cookie_name: str = Field(default="thedrop_session", alias="SESSION_COOKIE_NAME")
    # Used only by the seed script to create the first admin. Absent means no admin is
    # created -- the seed never invents a default credential.
    admin_email: str = Field(default="", alias="ADMIN_EMAIL")
    admin_initial_password: str = Field(default="", alias="ADMIN_INITIAL_PASSWORD")
    session_absolute_ttl_hours: int = Field(default=12, gt=0, alias="SESSION_ABSOLUTE_TTL_HOURS")
    session_idle_ttl_hours: int = Field(default=2, gt=0, alias="SESSION_IDLE_TTL_HOURS")

    api_internal_url: str = Field(default="http://127.0.0.1:8000", alias="API_INTERNAL_URL")
    web_port: int = Field(default=3100, alias="WEB_PORT")
    api_port: int = Field(default=8000, alias="API_PORT")

    # --- cross-origin deployment -------------------------------------------
    # On a single VPS the web app and API share an origin and none of this is used.
    # When the frontend is hosted separately (Vercel) and the API elsewhere
    # (Railway), the browser treats them as different origins and all three of
    # these must be set correctly or admin login silently fails.
    #
    # Comma-separated rather than JSON: these are edited by hand in a hosting
    # dashboard, where a malformed JSON array is an easy and confusing mistake.
    cors_allowed_origins_raw: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")
    trusted_hosts_raw: str = Field(default="", alias="TRUSTED_HOSTS")

    # e.g. ".thedrop.channel" so a cookie set by api.thedrop.channel is sent to
    # thedrop.channel. Both share a registrable domain, so they are "same-site"
    # and SameSite=Lax still applies -- no need for the far weaker SameSite=None.
    cookie_domain: str = Field(default="", alias="COOKIE_DOMAIN")

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts_raw.split(",") if h.strip()]

    @field_validator("cors_allowed_origins_raw")
    @classmethod
    def _no_wildcard_origin(cls, v: str) -> str:
        # A wildcard origin cannot be combined with credentialed requests: browsers
        # reject the response outright. Failing here beats debugging a silent
        # "login does nothing" in production.
        if "*" in v:
            msg = (
                "CORS_ALLOWED_ORIGINS must list explicit origins. "
                "A wildcard is incompatible with credentialed requests."
            )
            raise ValueError(msg)
        return v

    media_storage_backend: Literal["local", "s3"] = Field(
        default="local", alias="MEDIA_STORAGE_BACKEND"
    )
    media_root: Path = Field(default=_REPO_ROOT / "var" / "media", alias="MEDIA_ROOT")
    media_public_prefix: str = Field(default="/media", alias="MEDIA_PUBLIC_PREFIX")
    media_max_upload_mb: int = Field(default=8, gt=0, alias="MEDIA_MAX_UPLOAD_MB")

    ads_enabled: bool = Field(default=False, alias="ADS_ENABLED")

    editorial: EditorialSettings = Field(default_factory=EditorialSettings)
    ai: AISettings = Field(default_factory=AISettings)
    affiliate: AffiliateSettings = Field(default_factory=AffiliateSettings)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @field_validator("session_secret")
    @classmethod
    def _secret_is_real(cls, v: str, info: ValidationInfo) -> str:
        env = info.data.get("environment", Environment.DEVELOPMENT)
        if env is Environment.DEVELOPMENT:
            return v
        if len(v) < 32:
            msg = "SESSION_SECRET must be at least 32 characters outside development"
            raise ValueError(msg)
        if any(marker in v for marker in _PLACEHOLDER_MARKERS):
            msg = "SESSION_SECRET still contains a placeholder value"
            raise ValueError(msg)
        return v

    @field_validator("database_url", "redis_url")
    @classmethod
    def _no_placeholder_credentials(cls, v: object, info: ValidationInfo) -> object:
        env = info.data.get("environment", Environment.DEVELOPMENT)
        if env is Environment.DEVELOPMENT:
            return v
        rendered = str(v)
        if any(marker in rendered for marker in _PLACEHOLDER_MARKERS):
            msg = f"{info.field_name} still contains a placeholder credential"
            raise ValueError(msg)
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so validation runs exactly once."""
    return Settings()
