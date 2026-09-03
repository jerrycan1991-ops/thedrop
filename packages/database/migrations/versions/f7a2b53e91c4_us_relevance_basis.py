"""stories.us_relevance_basis

PIPELINE.md §7 specifies five weighted signals for US relevance; only two are
implemented (see thedrop_database.scoring), rescaled to fill the 0-100 stored score.
`us_relevance_basis` is what keeps that honest -- it records which signals ran, their
raw values, and what fraction of the documented formula's weight was actually covered,
the same role `sources.reliability_basis` plays for reliability_score.

Hand-written. Autogenerate against this schema also proposes dropping
`ix_raw_articles_fts`, created in raw SQL and invisible to the models. See 0ed9d87ee1ac.

Revision ID: f7a2b53e91c4
Revises: e58d1c7a40b2
Create Date: 2026-09-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a2b53e91c4"
down_revision: str | None = "e58d1c7a40b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "us_relevance_basis",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Which signals contributed to us_relevance_score, and what "
            "fraction of the documented formula's weight they cover.",
        ),
    )
    # The default only exists to satisfy existing rows during the ALTER TABLE; new
    # rows should be explicit, the same convention used for every other JSONB column
    # on this table.
    op.alter_column("stories", "us_relevance_basis", server_default=None)


def downgrade() -> None:
    op.drop_column("stories", "us_relevance_basis")
