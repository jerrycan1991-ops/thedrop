"""Users, roles and the append-only audit log."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import ActorType, SubscriptionTier

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_user_roles_role_id", "role_id"),
)


class Role(Base, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "roles"

    slug: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    users: Mapped[list[User]] = relationship(secondary=user_roles, back_populates="roles")

    def __repr__(self) -> str:
        return f"<Role {self.slug}>"


class User(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """Admin and (eventually) reader accounts.

    Passwords are argon2id. ``mfa_secret_enc`` is encrypted at rest with the session
    secret's KDF, never stored in the clear.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)

    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # Bumping this invalidates every existing session for the user. Used on password
    # change, role change, and during incident response.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    subscription_tier: Mapped[str] = mapped_column(
        String(16), default=SubscriptionTier.FREE, nullable=False
    )

    #: CANONICAL ROLE ORDERING: alphabetical by slug, resolved by the database.
    #:
    #: Without `order_by` this relationship returned rows in whatever order Postgres
    #: chose, which was never part of the contract only because no user had ever held
    #: more than one role. Defining it now, before a second assignment exists, is
    #: cheaper than discovering the non-determinism later.
    #:
    #: Why slug and not id: `roles.id` is seed insertion order. It currently reads
    #: admin, editor, analyst, viewer — privilege-descending by pure coincidence — and
    #: a role added next year would sort last regardless of what it means.
    #:
    #: Why not a priority column: it would need a migration, and it would imply a
    #: ranking the authorization code does not use. `require_role` is set membership
    #: with `admin` as an implicit superset; the array order carries no meaning, so the
    #: ordering should be neutral and stable rather than pretend to encode privilege.
    #:
    #: Both tiers sort in the DATABASE (here, and `ORDER BY r.slug` in the Node query),
    #: so they share one collation and cannot disagree.
    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", order_by="Role.slug"
    )

    def has_role(self, slug: str) -> bool:
        return any(r.slug == slug for r in self.roles)

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class AuditLog(Base, PrimaryKeyMixin):
    """Append-only. The application role is granted INSERT and SELECT only.

    Partitioned monthly in production (see the migration), retained 400 days.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor", "actor_type", "actor_id"),
    )

    actor_type: Mapped[str] = mapped_column(String(16), default=ActorType.SYSTEM, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type}:{self.entity_id}>"
