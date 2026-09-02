"""Migration history shape. No database required.

This exists because a merge produced two Alembic heads and nothing caught it until a
production deploy stopped on `upgrade head`:

    Multiple head revisions are present for given argument 'head'

Two migrations had been authored in parallel on separate branches, each naming the
same parent. Both branches passed their own tests; the defect was created by the
merge, which is precisely the case per-branch testing cannot see.

CLAUDE.md makes Alembic the ONLY schema migration authority. An authority with two
heads is not an authority -- it is two histories over one database, which is the exact
failure the rule was written to prevent, arrived at from inside Alembic rather than
from Drizzle.

These checks read the script directory only. They need no connection, so they run in
CI and on a laptop with no Postgres, which is where a head split should be caught.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "packages" / "database" / "alembic.ini"


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_exactly_one_head() -> None:
    """`upgrade head` must be unambiguous.

    If this fails, two revisions share a parent. The fix is to repoint the newer
    revision's ``down_revision`` at the older one -- NOT to run `alembic merge`,
    unless both branches genuinely reached a database already.
    """
    heads = _scripts().get_heads()
    assert len(heads) == 1, (
        f"Alembic has {len(heads)} heads: {sorted(heads)}. "
        "`alembic upgrade head` will refuse to run and the deploy will stop at "
        "migrations. Relink the newer revision onto the older."
    )


def test_history_is_a_single_chain() -> None:
    """One base, no branch points, and every parent resolvable.

    `get_heads` alone would still pass on a history that forked and re-merged. A
    single unbroken chain is what makes a rollback predictable: `downgrade -1` has
    exactly one meaning.
    """
    scripts = _scripts()
    revisions = list(scripts.walk_revisions())

    bases = [r.revision for r in revisions if r.down_revision is None]
    assert bases == [scripts.get_base()], f"expected exactly one base, found {bases}"

    parents: dict[str, str] = {}
    for revision in revisions:
        down = revision.down_revision
        assert not isinstance(down, tuple), (
            f"{revision.revision} is a merge revision ({down}). Allowed only when both "
            "branches were already applied to a real database; otherwise relink."
        )
        if down is None:
            continue
        assert scripts.get_revision(down) is not None, (
            f"{revision.revision} names a parent that does not exist: {down}"
        )
        assert down not in parents, (
            f"{down} has two children: {parents[down]} and {revision.revision}. "
            "That is a branch point -- see test_exactly_one_head."
        )
        parents[down] = revision.revision


def test_docstring_revises_matches_down_revision() -> None:
    """The header humans read must agree with the value Alembic obeys.

    Alembic writes ``Revises:`` into the docstring once and never looks at it again.
    Editing ``down_revision`` to fix a head split leaves the docstring asserting the
    old parent, so the file documents a history that is not the one in effect.
    """
    scripts = _scripts()
    for revision in scripts.walk_revisions():
        path = Path(revision.path)
        header = path.read_text(encoding="utf-8").split('"""', 2)[1]
        documented = next(
            (
                line.split(":", 1)[1].strip()
                for line in header.splitlines()
                if line.startswith("Revises:")
            ),
            None,
        )
        if documented is None:
            continue
        expected = revision.down_revision or ""
        assert documented == expected, (
            f"{path.name}: docstring says 'Revises: {documented}' but "
            f"down_revision is {expected!r}"
        )
