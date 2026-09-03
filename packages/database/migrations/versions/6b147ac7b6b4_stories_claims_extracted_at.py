"""stories.claims_extracted_at

`POST /api/v1/worker/claims` (services/api/app/routers/worker.py) needs somewhere to
record that claim extraction ran for a story, on every attempt -- not just a
successful one. A failed extraction leaves no `claims` rows behind, so without this
column, "failed" and "never attempted" are the same state, and a future dispatch query
built on "does this story have claims yet" would retry a consistently-failing story
forever instead of surfacing it. Same role `entities_extracted_at` plays for
`raw_articles` (0ed9d87ee1ac) and `scores_computed_at` already plays for this table.

Hand-written. Autogenerate against this schema also proposes dropping
`ix_raw_articles_fts`, created in raw SQL and invisible to the models. See 0ed9d87ee1ac.

Revision ID: 6b147ac7b6b4
Revises: ee52a30bb357
Create Date: 2026-09-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b147ac7b6b4"
down_revision: str | None = "ee52a30bb357"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "claims_extracted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When claim extraction last ran for this story, successfully or "
            "not. Null means never attempted.",
        ),
    )


def downgrade() -> None:
    op.drop_column("stories", "claims_extracted_at")
