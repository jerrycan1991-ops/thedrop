"""Atomic, checkable claims extracted from a story's source articles, and the
provenance tables that make every model call that produces them auditable
(PIPELINE.md §10-11, DATABASE.md §9).

A `Claim` on its own is an assertion to trust. `ClaimEvidence` is what makes it an
assertion to check -- the exact quote, article and source it came from. `AiRun` is
what makes the *extraction itself* auditable -- CLAUDE.md requires every model call
logged, without exception, and SECURITY.md §6.2 is explicit that the full prompt and
completion are a liability, not an asset, so only a digest is kept by default.
`PromptVersion` is what an `ai_runs` row resolves back to: the exact template that
produced it, not "whatever `services/agent-runner` happened to hardcode that week."
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
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import VerificationStatus


class PromptVersion(Base, PrimaryKeyMixin, TimestampMixin):
    """A versioned prompt template. Exactly one active version per `name`.

    SECURITY.md §6.1's SYSTEM channel is "static/versioned" -- read from here at call
    time, not assembled ad hoc in application code, so a prompt change is a reviewable
    row insert with a checksum, not an untracked edit to a Python f-string.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
        # Partial unique index: at most one row per name may have is_active=true.
        Index(
            "ix_prompt_versions_one_active",
            "name",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    model_hint: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    checksum: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<PromptVersion {self.name} v{self.version}>"


class AiRun(Base, PrimaryKeyMixin, TimestampMixin):
    """Every model call, without exception (CLAUDE.md).

    `cost` is nullable and left unset until `model_pricing` exists: DATABASE.md is
    explicit that per-token prices are configuration, never invented in code, so a run
    logged before that table is seeded records real token counts and an honest null
    cost rather than a fabricated number.
    """

    __tablename__ = "ai_runs"
    __table_args__ = (
        Index("ix_ai_runs_created_at", text("created_at DESC")),
        Index("ix_ai_runs_model_created_at", "model", text("created_at DESC")),
        Index("ix_ai_runs_article_id", "article_id"),
    )

    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    story_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stories.id", ondelete="SET NULL"), index=True
    )
    article_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="SET NULL")
    )
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )

    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6))

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    #: Hash of the request, not the request itself -- see the module docstring.
    request_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    response_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    def __repr__(self) -> str:
        return f"<AiRun {self.purpose} {self.model} {self.status}>"


class Claim(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """One atomic, checkable assertion extracted from a story's source articles.

    "Atomic" (one assertion, no conjunctions) is enforced at the prompt/schema level
    during extraction, not here -- this model stores what extraction produced. What IS
    enforced here is narrower but load-bearing: a claim of a type that asserts someone
    said or alleged something (`CLAIM`, `ALLEGATION`, `OFFICIAL_STATEMENT`) must name
    who. Without that check, a bug or a bad extraction could silently produce an
    unattributed allegation that a template has no way to distinguish from a fact.
    """

    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(
            "claim_type NOT IN ('CLAIM', 'ALLEGATION', 'OFFICIAL_STATEMENT') "
            "OR attributed_to_entity_id IS NOT NULL",
            name="ck_claims_attribution_required",
        ),
        Index("ix_claims_story_id", "story_id"),
        # The verification queue's working set: claims not yet resolved past
        # single-source, across every story at once.
        Index("ix_claims_verification_status", "verification_status"),
    )

    story_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stories.id", ondelete="CASCADE"), nullable=False
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(24), nullable=False)
    attributed_to_entity_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("entities.id", ondelete="SET NULL"), index=True
    )
    confidence: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(16), default=VerificationStatus.UNVERIFIED, nullable=False
    )
    is_load_bearing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supporting_source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Other claim ids and source refs this claim contradicts, or is contradicted by.
    contradicted_by: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    first_asserted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    verifier_ai_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_runs.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:
        return f"<Claim {self.claim_type} story={self.story_id}>"


class ClaimEvidence(Base, PrimaryKeyMixin):
    """The exact supporting (or contradicting) quote for one claim, tied to the
    specific article and source it came from. This is what makes a claim auditable
    rather than an assertion to trust on the model's word."""

    __tablename__ = "claim_evidence"
    __table_args__ = (Index("ix_claim_evidence_claim_id", "claim_id"),)

    claim_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    raw_article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_offset: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    is_primary_document: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    document_url: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Numeric(4, 3), default=1, nullable=False)

    def __repr__(self) -> str:
        return f"<ClaimEvidence claim={self.claim_id} stance={self.stance}>"
