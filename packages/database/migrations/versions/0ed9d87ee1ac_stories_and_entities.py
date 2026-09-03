"""stories, story_sources, entities and story_entities

Clustering's tables (PIPELINE.md 6). A `raw_article` is what one publisher said; a
`story` is the event they were all writing about.

`entities` and `story_entities` land in the SAME migration as `stories`, not later,
because the join rule requires a shared salient entity before two articles may cluster
together. Embeddings alone happily merge "shooting in Ohio" with "shooting in Nevada" --
PIPELINE.md calls that a correctness guard, not an optimization, so the table it reads
cannot arrive afterwards.

HAND-TRIMMED from autogenerate. The generated version also wanted to drop
`ix_raw_articles_fts`, swap `uq_raw_articles_public_id` for an index, and alter six
existing columns -- none of it in scope, and the first is destructive. The FTS index is
created in raw SQL (a1c7e2b40f13), so the models cannot see it and autogenerate reads
its absence from the metadata as an instruction to remove it. Every future autogenerate
against this schema will propose the same thing.

No HNSW index on `stories.centroid` yet. With zero rows a sequential scan wins, the
build cost lands on a 4-core VPS, and the right parameters depend on a row count that
does not exist. Same judgement as `raw_articles`.

Revision ID: 0ed9d87ee1ac
Revises: 258cd988cde6
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0ed9d87ee1ac"
down_revision: str | None = "258cd988cde6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("wikidata_id", sa.String(length=32), nullable=True),
        sa.Column("is_public_figure", sa.Boolean(), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        sa.UniqueConstraint("canonical_name", "entity_type", name="uq_entities_name_type"),
    )
    op.create_index("ix_entities_type", "entities", ["entity_type"], unique=False)
    op.create_table(
        "stories",
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("centroid", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("independent_source_count", sa.Integer(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("us_relevance_score", sa.SmallInteger(), nullable=True),
        sa.Column("viral_score", sa.SmallInteger(), nullable=True),
        sa.Column("opportunity_score", sa.SmallInteger(), nullable=True),
        sa.Column("importance_score", sa.SmallInteger(), nullable=True),
        sa.Column("credibility_score", sa.SmallInteger(), nullable=True),
        sa.Column("verification_confidence", sa.SmallInteger(), nullable=True),
        sa.Column("scores_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_tier", sa.String(length=16), nullable=False),
        sa.Column("risk_reasons", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("known_unknowns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contradictions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_packet", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_packet_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("defer_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_stories_category_id_categories"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stories")),
    )
    op.create_index(op.f("ix_stories_category_id"), "stories", ["category_id"], unique=False)
    op.create_index("ix_stories_last_activity", "stories", ["last_activity_at"], unique=False)
    op.create_index(op.f("ix_stories_public_id"), "stories", ["public_id"], unique=True)
    op.create_index(
        "ix_stories_status_activity", "stories", ["status", "last_activity_at"], unique=False
    )
    op.create_table(
        "story_entities",
        sa.Column("story_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("salience", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_story_entities_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_story_entities_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_entities")),
        sa.UniqueConstraint("story_id", "entity_id", name="uq_story_entities_pair"),
    )
    op.create_index("ix_story_entities_entity", "story_entities", ["entity_id"], unique=False)
    op.create_table(
        "story_sources",
        sa.Column("story_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_article_id", sa.BigInteger(), nullable=False),
        sa.Column("similarity", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("is_syndicated", sa.Boolean(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_article_id"],
            ["raw_articles.id"],
            name=op.f("fk_story_sources_raw_article_id_raw_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_story_sources_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_sources")),
        sa.UniqueConstraint("story_id", "raw_article_id", name="uq_story_sources_story_article"),
    )
    op.create_index("ix_story_sources_article", "story_sources", ["raw_article_id"], unique=False)


def downgrade() -> None:
    # Reverse creation order: story_entities and story_sources reference stories.
    op.drop_table("story_sources")
    op.drop_table("story_entities")
    op.drop_table("stories")
    op.drop_table("entities")
