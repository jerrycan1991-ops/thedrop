"""article provenance and traceability invariant

Adds the boundary that makes CLAUDE.md's traceability rule enforceable:

  * ``provenance`` -- 'manual' (a named human is accountable for the sentences) or
    'generated' (machine prose, which must carry claim traceability to go live).
  * ``traceability_verified_at`` -- set by editorial QA once every factual sentence
    resolved to a claim id with stored evidence.
  * a CHECK that a *generated* article cannot be live without that certification.

The constraint is deliberately vacuous today: nothing sets provenance='generated'
and the claims tables do not exist yet. That is the point. Adding it before the
generator exists means the generator is born compliant; adding it afterwards would
mean regenerating or retracting every article produced in between.

Revision ID: 258cd988cde6
Revises: bf45495a0cae
Create Date: 2026-09-01 22:06:25.465401

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "258cd988cde6"
down_revision: str | None = "bf45495a0cae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVENANCE_COMMENT = (
    "How this article was produced: 'manual' (a named human author is accountable) "
    "or 'generated' (must carry claim traceability to go live)."
)

_TRACEABILITY_COMMENT = (
    "Set by editorial QA once every factual sentence resolved to a claim id with "
    "stored evidence. Nothing writes it yet -- claims land in step 7."
)

# provenance='manual' OR the article is not live OR QA certified the trace.
_TRACEABILITY_CHECK = (
    "provenance <> 'generated' "
    "OR status NOT IN ('published', 'updated') "
    "OR traceability_verified_at IS NOT NULL"
)


def upgrade() -> None:
    # server_default backfills existing rows -- adding a NOT NULL column without one
    # fails outright on any table that already holds content. Autogenerate omitted it,
    # which would have passed on an empty development database and failed on a
    # populated one.
    op.add_column(
        "articles",
        sa.Column(
            "provenance",
            sa.String(length=16),
            nullable=False,
            server_default="manual",
            comment=_PROVENANCE_COMMENT,
        ),
    )

    # Then drop the default, so it exists only for the backfill above.
    #
    # Keeping it would be fail-open: 'manual' is precisely the value that escapes the
    # traceability constraint, so a generator that forgot to set provenance would
    # silently publish untraceable prose. Without a default, that same bug raises a
    # NOT NULL violation on insert. Every writer states its provenance explicitly.
    op.alter_column("articles", "provenance", server_default=None)

    op.add_column(
        "articles",
        sa.Column(
            "traceability_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=_TRACEABILITY_COMMENT,
        ),
    )

    op.create_check_constraint(
        op.f("ck_articles_provenance_values"),
        "articles",
        "provenance IN ('manual', 'generated')",
    )

    # 'updated' is included alongside 'published' because it also means live. Only
    # 'published' renders today -- both tiers filter on it -- but a status meaning
    # "live" must not become a hole in the invariant the day it enters the query.
    op.create_check_constraint(
        op.f("ck_articles_generated_live_requires_traceability"),
        "articles",
        _TRACEABILITY_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_articles_generated_live_requires_traceability"), "articles", type_="check"
    )
    op.drop_constraint(op.f("ck_articles_provenance_values"), "articles", type_="check")
    op.drop_column("articles", "traceability_verified_at")
    op.drop_column("articles", "provenance")
