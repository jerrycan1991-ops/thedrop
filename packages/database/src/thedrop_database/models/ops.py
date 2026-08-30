"""Desktop worker registry and the durable job-lease queue.

This is deliberately NOT Celery. The desktop is a remote worker on a home connection
that may vanish mid-task, so its work is first-class data: queryable, auditable,
restartable, and visible in the admin UI. See ADR-0003.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from thedrop_database.base import Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin
from thedrop_database.enums import JobStatus, WorkerStatus


class WorkerNode(Base, PrimaryKeyMixin, TimestampMixin):
    """A registered AI worker (the RTX 4070 SUPER desktop).

    The token is stored hashed, so a database leak yields no working credential.
    Two tokens may be valid during a rotation window.
    """

    __tablename__ = "worker_nodes"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, index=True)
    previous_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    token_rotated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    rotation_grace_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
        comment='e.g. {"gpu": true, "vram_gb": 12, "handlers": ["embed", "write"]}. '
        "The API only leases jobs a runner has advertised it can execute.",
    )

    status: Mapped[str] = mapped_column(
        String(16), default=WorkerStatus.OFFLINE, nullable=False, index=True
    )
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    current_job_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    gpu_name: Mapped[str | None] = mapped_column(String(128))
    gpu_vram_free_mb: Mapped[int | None] = mapped_column(Integer)
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip_last_seen: Mapped[str | None] = mapped_column(INET)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    jobs: Mapped[list[Job]] = relationship(back_populates="worker")

    def __repr__(self) -> str:
        return f"<WorkerNode {self.name} {self.status}>"


class Job(Base, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """A unit of work leased by the desktop.

    Claiming is a single ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)``,
    which is correct under concurrency without a distributed lock. A reaper returns
    jobs whose lease expired without a heartbeat.

    ``idempotency_key`` makes completion safe: a job that finished exactly as its lease
    expired cannot produce a duplicate article.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Covering index for the claim query. Partial, because the queue is mostly
        # historical rows and only 'queued' is ever scanned.
        Index(
            "ix_jobs_claimable",
            "priority",
            "available_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_status_created", "status", "created_at"),
        Index(
            "ix_jobs_lease_expiry",
            "lease_expires_at",
            postgresql_where=text("status = 'leased'"),
        ),
    )

    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    story_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    article_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=JobStatus.QUEUED, nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    leased_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("worker_nodes.id", ondelete="SET NULL")
    )
    leased_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    worker: Mapped[WorkerNode | None] = relationship(back_populates="jobs")

    def __repr__(self) -> str:
        return f"<Job {self.job_type} {self.status}>"
