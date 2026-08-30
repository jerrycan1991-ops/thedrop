"""Providers and sources.

A *provider* is an integration (GNews, RSS, a government feed). A *source* is a
publisher whose articles arrive through one or more providers. The distinction
matters: one provider may deliver content from hundreds of sources, and credibility
attaches to the source, not the pipe it arrived through.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thedrop_database.base import Base, PrimaryKeyMixin, TimestampMixin
from thedrop_database.enums import CircuitState, SourceType


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

    default_reliability: Mapped[float] = mapped_column(
        Numeric(4, 3), default=0.500, nullable=False
    )

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
    reliability_score: Mapped[float] = mapped_column(
        Numeric(4, 3), default=0.400, nullable=False
    )
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
