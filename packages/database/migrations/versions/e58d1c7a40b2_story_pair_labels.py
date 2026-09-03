"""story_pair_labels

The other half of Phase 3's exit criterion. `cluster_labels` judges placements that
happened, so it can only find the threshold too loose; it is blind by construction to
articles that should have joined and did not. That is the failure this design
deliberately courts, since over-splitting is its chosen safe direction, and after 71
placements with zero errors it is the only remaining question.

`similarity` and `shared_entities` are recorded with the verdict so a pair a human calls
one event says WHICH condition kept them apart -- the threshold or the entity guard.
Without that the measurement would give a number and no diagnosis.

Revision ID: e58d1c7a40b2
Revises: c4a71f0e93bd
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e58d1c7a40b2"
down_revision: str | None = "c4a71f0e93bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "story_pair_labels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("story_id", sa.BigInteger(), nullable=False),
        sa.Column("other_story_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("similarity", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("shared_entities", sa.Integer(), nullable=True),
        sa.Column("labelled_by", sa.String(length=64), nullable=True),
        sa.Column(
            "labelled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Lower id first, enforced by the database: one judgement is recorded once
        # however the pair is encountered.
        sa.CheckConstraint("story_id < other_story_id", name="ck_story_pair_labels_ordered"),
        sa.ForeignKeyConstraint(
            ["other_story_id"],
            ["stories.id"],
            name=op.f("fk_story_pair_labels_other_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_story_pair_labels_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_pair_labels")),
        sa.UniqueConstraint("story_id", "other_story_id", name="uq_story_pair_labels_pair"),
    )
    op.create_index("ix_story_pair_labels_other", "story_pair_labels", ["other_story_id"])


def downgrade() -> None:
    op.drop_index("ix_story_pair_labels_other", table_name="story_pair_labels")
    op.drop_table("story_pair_labels")
