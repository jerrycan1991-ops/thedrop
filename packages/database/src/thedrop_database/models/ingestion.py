"""Providers and sources.

A *provider* is an integration (GNews, RSS, a government feed). A *source* is a
publisher whose articles arrive through one or more providers. The distinction
matters: one provider may deliver content from hundreds of sources, and credibility
attaches to the source, not the pipe it arrived through.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import CircuitState, DedupStatus, IngestStatus, SourceType


class Provider(Base, PrimaryKeyMixin, TimestampMixin):
    """An ingestion adapter registration.

    ``credential_ref`` names a key in the secret store. The secret itself is never
    stored in the database.
    """

    __tablename__ = "providers"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    adapter_class: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Dotted path resolved at runtime."
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(128))

    rate_limit_per_hour: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    quota_used_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)

    default_reliability: Mapped[float] = mapped_column(Numeric(4, 3), default=0.500, nullable=False)

    circuit_state: Mapped[str] = mapped_column(
        String(16), default=CircuitState.CLOSED, nullable=False
    )
    circuit_opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Provider {self.slug} enabled={self.enabled}>"


class Source(Base, PrimaryKeyMixin, TimestampMixin):
    """A publisher.

    New domains are auto-created on first sight with ``allow_auto_publish=False``.
    A source starts untrusted: it can contribute context, but a story cannot rely on
    it alone to satisfy a corroboration requirement until it has been classified.
    """

    __tablename__ = "sources"
    __table_args__ = (Index("ix_sources_reliability", "reliability_score"),)

    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(String(2), default="US", nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    source_type: Mapped[str] = mapped_column(
        String(24), default=SourceType.UNKNOWN, nullable=False, index=True
    )
    reliability_score: Mapped[float] = mapped_column(Numeric(4, 3), default=0.400, nullable=False)
    reliability_basis: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False, comment="Inputs and last recomputation."
    )

    bias_label: Mapped[str | None] = mapped_column(
        String(32),
        comment="Recorded for balance reporting. NEVER used to suppress a source.",
    )
    is_primary_authority: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True for .gov, courts, regulators and official organisation statements.",
    )
    allow_auto_publish: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    robots_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    correction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contradiction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return f"<Source {self.domain} r={self.reliability_score}>"


class RawArticle(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """Immutable capture of one ingested item. Never edited after insert.

    This is evidence, not content. Nothing here is rendered to a reader: articles are
    generated from a structured evidence packet, never rewritten from source prose
    (CLAUDE.md, copyright). `image_urls` holds references only -- an image is never
    rehosted.

    Text that addresses the system ("ignore previous instructions", "publish this as
    breaking") is recorded in `injection_flags` and kept. Flagged content is still
    stored because it is evidence; it is simply never treated as instruction (ADR-0008).

    Schema follows DATABASE.md's `raw_articles` table exactly.
    """

    __tablename__ = "raw_articles"
    __table_args__ = (
        Index("ix_raw_articles_discovered_at", text("discovered_at DESC")),
        Index("ix_raw_articles_story_id", "story_id"),
        # Partial: the dedup sweep only ever scans pending rows, and this table becomes
        # the largest in the database (~2-5k rows/day).
        Index(
            "ix_raw_articles_dedup_pending",
            "dedup_status",
            postgresql_where=text("dedup_status = 'pending'"),
        ),
        # SimHash lookup is by 16-bit band, so the column itself needs to be indexed.
        Index("ix_raw_articles_simhash", "simhash"),
    )

    provider_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # No ForeignKey: `stories` does not exist until Phase 3, and Job.story_id sets the
    # same precedent. It becomes a real FK when the table lands.
    story_id: Mapped[int | None] = mapped_column(BigInteger)

    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256(canonical_url). THE primary dedup guard -- an insert conflict here is the
    #: cheapest possible duplicate detection, and it is a database constraint rather
    #: than application logic so two concurrent pollers cannot both win.
    url_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    dek: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html_sanitized: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)

    #: Source-reported. When absent, normalization falls back to discovery time and
    #: records that in raw_payload, because an invented timestamp is a fabricated fact.
    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovered_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    #: References only. Never rehosted, never traced, never recreated.
    image_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)

    #: The provider's full response, for replay. Lets a normalization bug be re-run
    #: against real input instead of re-fetched from an API that has moved on.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    #: 64-bit SimHash over title + first 400 chars, stored signed because Postgres has
    #: no unsigned bigint. Compared by Hamming distance, never by magnitude.
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    #: sha256 of the normalized body -- catches identical syndication under different
    #: URLs, which the url_hash constraint cannot see.
    content_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))

    #: Written by the desktop (ADR-0005), null until then. The VPS never computes this.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    embedded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set when entity extraction has RUN, which is not the same as having found
    #: something. Without it an article with no recognisable entities is
    #: indistinguishable from one never processed, and would be re-dispatched forever.
    entities_extracted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    dedup_status: Mapped[str] = mapped_column(
        String(16), default=DedupStatus.PENDING, nullable=False
    )
    duplicate_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("raw_articles.id", ondelete="SET NULL"), index=True
    )

    #: Prompt-injection scan results (SECURITY.md §6). Empty dict means scanned-and-clean;
    #: the column is NOT NULL so "never scanned" is distinguishable from "clean".
    injection_flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    ingest_status: Mapped[str] = mapped_column(
        String(16), default=IngestStatus.RAW, nullable=False, index=True
    )
    reject_reason: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<RawArticle {self.canonical_url[:60]} {self.dedup_status}>"
