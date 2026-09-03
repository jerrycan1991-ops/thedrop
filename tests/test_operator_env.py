"""Loading the operator env file for a hand-run command.

Exists because every operational check on the VPS had to be prefixed with
`set -a; . ~/.config/thedrop/thedrop.env; set +a`, and forgetting it produced a psycopg
traceback naming a localhost default rather than the actual cause. It was forgotten
repeatedly, including by whoever was writing the instructions.

The behaviours worth pinning are the ones that would make it dangerous rather than
merely useless: it must not overwrite what is already set, and it must parse values the
way `source` does.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from thedrop_database.operator_env import load_operator_env


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "thedrop.env"
    path.write_text(body, encoding="utf-8")
    return path


def test_it_loads_when_database_url_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env = write_env(tmp_path, "DATABASE_URL=postgresql://from-file/db\nREDIS_URL=redis://x\n")

    used = load_operator_env(env)

    assert used == env
    assert os.environ["DATABASE_URL"] == "postgresql://from-file/db"
    assert os.environ["REDIS_URL"] == "redis://x"


def test_an_existing_value_always_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone who passed DATABASE_URL on the command line meant it. Overwriting would
    point a hand-run command at a different database than the one they named."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://explicit/db")
    env = write_env(tmp_path, "DATABASE_URL=postgresql://from-file/db\n")

    assert load_operator_env(env) is None
    assert os.environ["DATABASE_URL"] == "postgresql://explicit/db"


def test_a_missing_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal case on a development machine, where the repository's own .env is
    where configuration comes from."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert load_operator_env(tmp_path / "absent.env") is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('KEY="quoted value"', "quoted value"),
        ("KEY='single quoted'", "single quoted"),
        ("KEY=bare", "bare"),
        ("KEY=  padded  ", "padded"),
        ('KEY=ends-with-a-quote"', 'ends-with-a-quote"'),
        ("KEY=has=equals=inside", "has=equals=inside"),
    ],
    ids=["double", "single", "bare", "padded", "trailing quote", "equals in value"],
)
def test_values_parse_the_way_source_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, expected: str
) -> None:
    """Only a MATCHING pair of quotes is stripped. `strip('"')` would eat a quote that
    is genuinely part of the value -- and a password is exactly where that would
    happen, silently, producing an authentication failure with no visible cause.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KEY", raising=False)
    env = write_env(tmp_path, f"DATABASE_URL=x\n{line}\n")

    load_operator_env(env)

    assert os.environ["KEY"] == expected


def test_comments_and_blank_lines_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REAL", raising=False)
    env = write_env(tmp_path, "# a comment\n\nDATABASE_URL=x\nREAL=yes\nnot-a-pair\n")

    load_operator_env(env)

    assert os.environ["REAL"] == "yes"


def test_the_operator_commands_import() -> None:
    """A syntax error in these was caught by ruff, not by 495 tests, because nothing
    imported them. They are the commands an operator reaches for when something is
    already wrong, so discovering they are broken at that moment is the worst case.

    Importing is a low bar and it is the bar that was missed.
    """
    import thedrop_database.pipeline_status as status

    assert callable(status.main)
    assert callable(status.show_clusters)
    # The queries are module constants, so a truncated edit shows up here rather than
    # as an empty report that looks like an empty database.
    assert "raw_articles" in status._STAGES
    assert "story_sources" in status._CLUSTERS
    assert "merged_into_id" not in status._STAGES
