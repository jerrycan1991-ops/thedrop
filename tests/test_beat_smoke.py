"""The deploy gate for scheduled tasks.

It exists because `dispatch_embedding_batches` raised on every 120-second tick while
two consecutive deploys reported six green gates. A beat task that raises leaves the
worker process perfectly stable — Celery catches the exception, logs it, and waits for
the next tick — so every existing gate answered "healthy" truthfully while the
scheduled work was dead.

These cover the judgement in the gate, not Celery: which tasks it chooses to run, that
a raising task fails the deploy, and that finding nothing is a failure rather than a
pass. The real Celery app cannot be imported here — services/api and services/worker
both ship a top-level `app`, and conftest puts services/api first — so the app is a
stub shaped like the parts the gate touches.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "infrastructure" / "scripts"))

from beat_smoke import interval_tasks, run_smoke, skipped_tasks  # noqa: E402


class Crontab:
    """Stands in for `celery.schedules.crontab` — anything not a plain number."""

    def __repr__(self) -> str:
        return "<crontab 0 5 * * *>"


class FakeConf:
    def __init__(self, beat_schedule: dict[str, Any], include: list[str] | None = None) -> None:
        self.beat_schedule = beat_schedule
        self.include = include or []


class FakeApp:
    def __init__(self, beat_schedule: dict[str, Any], tasks: dict[str, Any]) -> None:
        self.conf = FakeConf(beat_schedule)
        self.tasks = tasks


SCHEDULE = {
    "dispatch-due-providers": {"task": "app.tasks.ingest.dispatch_due_providers", "schedule": 60.0},
    "dispatch-embedding-batches": {
        "task": "app.tasks.embed.dispatch_embedding_batches",
        "schedule": 120.0,
    },
    "reset-provider-quotas": {
        "task": "app.tasks.maintain.reset_provider_quotas",
        "schedule": Crontab(),
    },
}


# ------------------------------------------------------------------- selection


def test_interval_scheduled_tasks_are_selected() -> None:
    selected = {task for _, task in interval_tasks(SCHEDULE)}
    assert selected == {
        "app.tasks.ingest.dispatch_due_providers",
        "app.tasks.embed.dispatch_embedding_batches",
    }


def test_cron_scheduled_tasks_are_excluded() -> None:
    """`reset_provider_quotas` runs at 00:05 for a reason: it zeroes per-provider daily
    counters, which is a rate-limiting safeguard. A crontab names a TIME, so there is no
    moment during a deploy at which running it early is equivalent to letting it run.
    """
    selected = {task for _, task in interval_tasks(SCHEDULE)}
    assert "app.tasks.maintain.reset_provider_quotas" not in selected


def test_excluded_tasks_are_reported_not_hidden() -> None:
    """A silently skipped task is indistinguishable from a passing one, which is the
    exact failure this gate exists to prevent."""
    skipped = {task for _, task in skipped_tasks(SCHEDULE)}
    assert skipped == {"app.tasks.maintain.reset_provider_quotas"}


def test_a_boolean_schedule_is_not_mistaken_for_an_interval() -> None:
    """`True` is an int in Python. Left unguarded it would select a nonsense entry and
    then invoke it."""
    assert interval_tasks({"odd": {"task": "app.tasks.x", "schedule": True}}) == []


# --------------------------------------------------------------------- running


def test_a_task_that_raises_is_reported() -> None:
    """The whole point. This is the shape of the failure that shipped: an
    AttributeError inside the task body, invisible to every other gate."""

    def broken() -> None:
        raise AttributeError("'Settings' object has no attribute 'embedding_batch_size'")

    app = FakeApp(
        SCHEDULE,
        {
            "app.tasks.ingest.dispatch_due_providers": lambda: {"dispatched": []},
            "app.tasks.embed.dispatch_embedding_batches": broken,
        },
    )

    failures = run_smoke(app)

    assert len(failures) == 1
    task_name, reason = failures[0]
    assert task_name == "app.tasks.embed.dispatch_embedding_batches"
    assert "embedding_batch_size" in reason


def test_healthy_tasks_produce_no_failures() -> None:
    app = FakeApp(
        SCHEDULE,
        {
            "app.tasks.ingest.dispatch_due_providers": lambda: {"dispatched": []},
            "app.tasks.embed.dispatch_embedding_batches": lambda: {"queued": 0},
        },
    )

    assert run_smoke(app) == []


def test_a_scheduled_task_that_is_not_registered_fails() -> None:
    """Beat resolves names against the registry. A task scheduled but never imported is
    published every tick and consumed by nobody -- which looks like silence, not error.
    """
    app = FakeApp(SCHEDULE, {"app.tasks.ingest.dispatch_due_providers": lambda: None})

    failures = run_smoke(app)

    assert [name for name, _ in failures] == ["app.tasks.embed.dispatch_embedding_batches"]
    assert "not registered" in failures[0][1]


def test_the_real_beat_schedule_has_interval_tasks_to_cover() -> None:
    """Guards the gate against becoming vacuous.

    `main()` exits non-zero when it discovers nothing, but that only fires at deploy
    time. This reads the deployed schedule's shape from source, so a refactor that moved
    every task to a crontab would fail here rather than turning the gate into a no-op
    that reports success.
    """
    source = (REPO_ROOT / "services" / "worker" / "app" / "celery_app.py").read_text(
        encoding="utf-8"
    )
    assert '"schedule": 60.0' in source or '"schedule": 120.0' in source


@pytest.mark.parametrize("schedule", [{}, {"x": {"schedule": 60.0}}], ids=["empty", "no task name"])
def test_nothing_to_run_selects_nothing(schedule: dict[str, Any]) -> None:
    assert interval_tasks(schedule) == []
