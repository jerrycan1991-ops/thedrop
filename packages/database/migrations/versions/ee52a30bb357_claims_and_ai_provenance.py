"""prompt_versions, ai_runs, claims and claim_evidence

Entity and claim extraction's tables (PIPELINE.md 10-11, DATABASE.md 9). This is the
first stage in the pipeline that calls a model over source content, so the provenance
tables land in the same migration as the domain tables they support -- an ai_runs row
with nowhere to point (no claims table yet) would be exactly the kind of "logged but
unauditable" gap CLAUDE.md's traceability rule exists to prevent.

Table order follows the dependency chain: prompt_versions has no FKs, ai_runs
references it (plus jobs/stories/articles, all pre-existing), claims references
ai_runs (verifier_ai_run_id) and entities (attributed_to_entity_id), claim_evidence
references claims/raw_articles/sources.

`claims.claim_type`, `verification_status`, and every other closed-set column here are
plain VARCHAR, not a native Postgres ENUM -- matching entities.entity_type in
0ed9d87ee1ac and enums.py's stated convention: adding a value to a PG enum type takes a
migration and a table lock; a value in an application StrEnum does not.

The `ck_claims_attribution_required` constraint is the one piece of real logic in this
migration: PIPELINE.md 10 requires CLAIM/ALLEGATION/OFFICIAL_STATEMENT to name who
asserted the thing, because PIPELINE.md 11's "Person X claims Y" must never silently
become "Y happened" -- a constraint the app can accidentally skip is not a constraint.

No HNSW-style "wait for real row counts" deferral needed here -- none of these four
tables get a vector or full-text index in this migration.

Revision ID: ee52a30bb357
Revises: f7a2b53e91c4
Create Date: 2026-09-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ee52a30bb357"
down_revision: str | None = "f7a2b53e91c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_hint", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("checksum", sa.LargeBinary(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
        sa.UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
    )
    op.create_index(
        "ix_prompt_versions_one_active",
        "prompt_versions",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "ai_runs",
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("story_id", sa.BigInteger(), nullable=True),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column("prompt_version_id", sa.BigInteger(), nullable=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("response_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_ai_runs_article_id_articles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_ai_runs_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_ai_runs_prompt_version_id_prompt_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_ai_runs_story_id_stories"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_runs")),
    )
    op.create_index("ix_ai_runs_article_id", "ai_runs", ["article_id"], unique=False)
    op.create_index("ix_ai_runs_created_at", "ai_runs", [sa.text("created_at DESC")], unique=False)
    op.create_index(op.f("ix_ai_runs_job_id"), "ai_runs", ["job_id"])
    op.create_index(
        "ix_ai_runs_model_created_at",
        "ai_runs",
        ["model", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(op.f("ix_ai_runs_story_id"), "ai_runs", ["story_id"], unique=False)

    op.create_table(
        "claims",
        sa.Column("story_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.String(length=24), nullable=False),
        sa.Column("attributed_to_entity_id", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.SmallInteger(), nullable=False),
        sa.Column("verification_status", sa.String(length=16), nullable=False),
        sa.Column("is_load_bearing", sa.Boolean(), nullable=False),
        sa.Column("supporting_source_count", sa.Integer(), nullable=False),
        sa.Column("contradicted_by", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_asserted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verifier_ai_run_id", sa.BigInteger(), nullable=True),
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
        sa.CheckConstraint(
            "claim_type NOT IN ('CLAIM', 'ALLEGATION', 'OFFICIAL_STATEMENT') "
            "OR attributed_to_entity_id IS NOT NULL",
            name="ck_claims_attribution_required",
        ),
        sa.ForeignKeyConstraint(
            ["attributed_to_entity_id"],
            ["entities.id"],
            name=op.f("fk_claims_attributed_to_entity_id_entities"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_claims_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verifier_ai_run_id"],
            ["ai_runs.id"],
            name=op.f("fk_claims_verifier_ai_run_id_ai_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
    )
    op.create_index(
        op.f("ix_claims_attributed_to_entity_id"), "claims", ["attributed_to_entity_id"]
    )
    op.create_index(op.f("ix_claims_public_id"), "claims", ["public_id"], unique=True)
    op.create_index("ix_claims_story_id", "claims", ["story_id"], unique=False)
    op.create_index(
        "ix_claims_verification_status", "claims", ["verification_status"], unique=False
    )
    op.create_index(op.f("ix_claims_verifier_ai_run_id"), "claims", ["verifier_ai_run_id"])

    op.create_table(
        "claim_evidence",
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_article_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_offset", sa.Integer(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("is_primary_document", sa.Boolean(), nullable=False),
        sa.Column("document_url", sa.Text(), nullable=True),
        sa.Column("weight", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_evidence_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_article_id"],
            ["raw_articles.id"],
            name=op.f("fk_claim_evidence_raw_article_id_raw_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_claim_evidence_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_evidence")),
    )
    op.create_index("ix_claim_evidence_claim_id", "claim_evidence", ["claim_id"], unique=False)
    op.create_index(op.f("ix_claim_evidence_raw_article_id"), "claim_evidence", ["raw_article_id"])
    op.create_index(op.f("ix_claim_evidence_source_id"), "claim_evidence", ["source_id"])


def downgrade() -> None:
    # Reverse creation order: each table references the one(s) created before it.
    op.drop_table("claim_evidence")
    op.drop_table("claims")
    op.drop_table("ai_runs")
    op.drop_table("prompt_versions")
