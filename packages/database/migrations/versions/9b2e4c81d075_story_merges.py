"""stories.merged_into_id

Consolidation merges stories that clustering split apart. The losing row is kept, not
deleted: PIPELINE.md requires merges to be recorded so a story's identity is auditable,
and a deleted row cannot explain where its articles went.

Self-referential, `ON DELETE SET NULL`. A cascade would be wrong -- deleting a survivor
must not take its history with it.

Hand-written. Autogenerate against this schema also proposes dropping
`ix_raw_articles_fts`, which is created in raw SQL and therefore invisible to the
models. See 0ed9d87ee1ac.

Revision ID: 9b2e4c81d075
Revises: 7c3f9a21be40
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b2e4c81d075"
down_revision: str | None = "7c3f9a21be40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "merged_into_id",
            sa.BigInteger(),
            nullable=True,
            comment="Set when this story was merged into another. The row is kept so "
            "the merge stays auditable.",
        ),
    )
    op.create_foreign_key(
        op.f("fk_stories_merged_into_id_stories"),
        "stories",
        "stories",
        ["merged_into_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_stories_merged_into_id"), "stories", ["merged_into_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_stories_merged_into_id"), table_name="stories")
    op.drop_constraint(op.f("fk_stories_merged_into_id_stories"), "stories", type_="foreignkey")
    op.drop_column("stories", "merged_into_id")
