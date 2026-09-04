"""stories.contradictions_checked_at

`POST /api/v1/worker/contradictions` (services/api/app/routers/worker.py) needs
somewhere to record that contradiction-checking ran for a story, on every attempt --
not just a successful one, matching `claims_extracted_at`'s role (6b147ac7b6b4). A
failed check leaves no changed claims behind, so without this column, "failed" and
"never attempted" are the same state, and a dispatch query built on "any disputed/
refuted claims yet" would retry a consistently-failing story forever.

Hand-written. Autogenerate against this schema also proposes dropping
`ix_raw_articles_fts`, created in raw SQL and invisible to the models. See 0ed9d87ee1ac.

Revision ID: e59b27d76f0b
Revises: 6b147ac7b6b4
Create Date: 2026-09-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e59b27d76f0b"
down_revision: str | None = "6b147ac7b6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "contradictions_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When this story's claims were last checked against each other "
            "for contradictions, successfully or not. Null means never attempted.",
        ),
    )


def downgrade() -> None:
    op.drop_column("stories", "contradictions_checked_at")
