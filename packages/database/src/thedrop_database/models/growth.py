"""Newsletter and advertising placement."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import SubscriberStatus


class NewsletterSubscriber(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """Double opt-in, stored in our own table from day one.

    The list is ours and portable. Sending is added later behind a provider interface;
    a list locked inside a vendor is a liability, so ownership comes first.
    """

    __tablename__ = "newsletter_subscribers"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SubscriberStatus.PENDING, nullable=False, index=True
    )
    confirm_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), index=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), default="site", nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(128))

    def __repr__(self) -> str:
        return f"<NewsletterSubscriber {self.status}>"


class AdPlacement(Base, PrimaryKeyMixin, TimestampMixin):
    """A configured ad slot.

    Business logic never imports an ad network. A slot resolves to a provider at
    render time, and renders *nothing* (not an empty box) when ineligible, so layout
    cannot shift.

    ``excluded_risk_tiers`` defaults to blocking ads on high-risk stories -- deaths,
    crime, war, tragedy. That protects readers and the ad account alike.
    """

    __tablename__ = "ad_placements"

    slot_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    min_height_px: Mapped[int] = mapped_column(
        Integer,
        default=250,
        nullable=False,
        comment="Space is reserved before load so ads cannot damage CLS.",
    )

    categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    article_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    excluded_risk_tiers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: ["high"], nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return f"<AdPlacement {self.slot_key} {self.provider}>"
