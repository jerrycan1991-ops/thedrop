"""raw_article_entities and the extraction marker

Entity extraction (PIPELINE.md 12) runs on the desktop and writes here.

`story_entities` cannot serve the clustering guard on its own. The guard compares an
incoming article's entities against a candidate story's entities *before* deciding
whether the article joins it, so at the moment of comparison the article has no story.
`raw_article_entities` is the article side of that comparison, resolving to the same
`entities` rows the story side uses -- so the guard matches on entity_id rather than on
approximate string equality.

`raw_articles.entities_extracted_at` marks that extraction RAN, which is not the same
as having found something. Without it, an article containing no recognisable entities
is indistinguishable from one never processed, and the dispatcher would queue it on
every tick forever.

Hand-written. Autogenerate against this schema also proposes dropping
`ix_raw_articles_fts` -- created in raw SQL, so invisible to the models -- along with
unrelated churn on existing columns. See 0ed9d87ee1ac.

Revision ID: 7c3f9a21be40
Revises: 0ed9d87ee1ac
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c3f9a21be40"
down_revision: str | None = "0ed9d87ee1ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "raw_articles",
        sa.Column(
            "entities_extracted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When entity extraction ran. Null means never processed; set with "
            "no rows in raw_article_entities means processed and nothing found.",
        ),
    )

    op.create_table(
        "raw_article_entities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("raw_article_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "salience",
            sa.Numeric(precision=4, scale=3),
            nullable=True,
            comment="Share of this article's entity mentions that were this entity. "
            "Centrality, not model confidence.",
        ),
        sa.Column("mention_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_raw_article_entities_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_article_id"],
            ["raw_articles.id"],
            name=op.f("fk_raw_article_entities_raw_article_id_raw_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_article_entities")),
        sa.UniqueConstraint(
            "raw_article_id", "entity_id", name="uq_raw_article_entities_pair"
        ),
    )
    # The unique constraint's index leads with raw_article_id and serves lookups by
    # article. This covers the other direction -- every foreign key gets an index.
    op.create_index(
        "ix_raw_article_entities_entity", "raw_article_entities", ["entity_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_raw_article_entities_entity", table_name="raw_article_entities")
    op.drop_table("raw_article_entities")
    op.drop_column("raw_articles", "entities_extracted_at")
