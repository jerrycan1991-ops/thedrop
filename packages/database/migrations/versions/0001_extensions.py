"""Enable required PostgreSQL extensions.

Revision ID: 0001_extensions
Revises:
Create Date: 2026-08-30

Extensions are their own revision, ahead of any table, for two reasons:

  * ``vector`` must exist before any ``vector(384)`` column is created (Phase 3), and
    a failed extension creation mid-schema leaves a half-built database.
  * ``pg_trgm`` backs the cheap trigram deduplication that runs on the VPS in place of
    embeddings (ADR-0005).

Requires the ``pgvector/pgvector:pg16`` image (or pgvector installed on the host).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # Deliberately not dropped. Dropping an extension cascades to every dependent
    # column, which would silently destroy data on a routine downgrade.
    pass
