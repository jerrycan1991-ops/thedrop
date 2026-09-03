"""cluster_labels

Ground truth for the Phase 3 exit criterion: clustering precision >= 0.90 on a
hand-labelled set of at least 200 articles.

One row per PLACEMENT -- an article joining a story -- because that is the decision the
guard makes. A founder is not a placement and is not labelled; counting founders would
inflate precision with decisions nobody took.

In the database rather than a file so labels survive a redeploy and can be joined
against what they judge. Labels are evidence, and evidence that can be regenerated is
not evidence.

Revision ID: c4a71f0e93bd
Revises: 9b2e4c81d075
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a71f0e93bd"
down_revision: str | None = "9b2e4c81d075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cluster_labels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("story_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_article_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "verdict",
            sa.String(length=16),
            nullable=False,
            comment="correct | wrong | unsure. 'unsure' is recorded, not skipped: "
            "dropping it would bias the measurement towards the easy cases.",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("labelled_by", sa.String(length=64), nullable=True),
        sa.Column(
            "labelled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_article_id"],
            ["raw_articles.id"],
            name=op.f("fk_cluster_labels_raw_article_id_raw_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_cluster_labels_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cluster_labels")),
        sa.UniqueConstraint("story_id", "raw_article_id", name="uq_cluster_labels_placement"),
    )
    op.create_index("ix_cluster_labels_article", "cluster_labels", ["raw_article_id"])


def downgrade() -> None:
    op.drop_index("ix_cluster_labels_article", table_name="cluster_labels")
    op.drop_table("cluster_labels")
