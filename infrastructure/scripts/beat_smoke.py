"""Run every interval-scheduled Celery task once, and fail if any of them raises.

    PYTHONPATH=services/worker python infrastructure/scripts/beat_smoke.py

WHY THIS EXISTS

`dispatch_embedding_batches` shipped reading a setting off the wrong object and raised
on every 120-second tick. Two deploys reported six green gates while it crash-looped,
because every gate answers a different question: HTTP health, route ownership, static
assets, migrations, and whether the worker PROCESS is restarting. A beat task that
raises leaves the worker perfectly stable — it catches the exception, logs it, and
waits for the next tick. Nothing outside the log ever notices.

So this asks the question none of the others do: can the scheduled work actually run?

WHY IT INVOKES RATHER THAN WATCHES

Waiting for the schedule and scanning the log would mean holding the deploy open for at
least one full interval of the slowest task — and then trusting a log grep. Calling the
task body directly is deterministic, costs a second, and tests the same code path the
scheduler will take.

The side effects are the ones the schedule produces anyway, a minute or two early: a
provider fan-out, a lease reap, a batch of embedding jobs queued. All are idempotent by
construction, because they already run repeatedly on a timer.

WHY CRON-SCHEDULED TASKS ARE EXCLUDED

`reset_provider_quotas` runs at 00:05 for a reason. Invoking it mid-deploy would zero
the per-provider daily counters hours early, which is a rate-limiting safeguard, not a
housekeeping detail — CLAUDE.md is explicit that a safeguard is never weakened to make
something else work. A `crontab` schedule means "at this time", so there is no moment
during a deploy at which running it is equivalent to letting it run. Interval schedules
carry no such claim.

The task list is derived from `beat_schedule`, so a new scheduled task is covered
automatically. There is no second list to keep in sync.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]


class CeleryLike(Protocol):
    conf: Any
    tasks: Any


def interval_tasks(beat_schedule: dict[str, Any]) -> list[tuple[str, str]]:
    """(entry name, task name) for entries on a numeric interval.

    Anything whose schedule is not a plain number — a `crontab`, a `solar` — is
    excluded, because those name a TIME rather than a frequency and running them early
    is a behaviour change rather than an early tick.
    """
    selected: list[tuple[str, str]] = []
    for entry_name, entry in sorted(beat_schedule.items()):
        schedule = entry.get("schedule")
        task_name = entry.get("task")
        if not task_name:
            continue
        if isinstance(schedule, bool) or not isinstance(schedule, (int, float)):
            continue
        selected.append((entry_name, str(task_name)))
    return selected


def skipped_tasks(beat_schedule: dict[str, Any]) -> list[tuple[str, str]]:
    """The complement of `interval_tasks`, reported so exclusions stay visible.

    A silently skipped task is indistinguishable from a passing one, which is the exact
    failure this whole script exists to prevent.
    """
    covered = {task for _, task in interval_tasks(beat_schedule)}
    return [
        (name, str(entry.get("task")))
        for name, entry in sorted(beat_schedule.items())
        if entry.get("task") and str(entry["task"]) not in covered
    ]


def run_smoke(app: CeleryLike) -> list[tuple[str, str]]:
    """Invoke each interval task once. Returns [(task name, failure)], empty on success."""
    for module in getattr(app.conf, "include", None) or []:
        # Beat resolves task names against the registry, which is only populated once
        # the modules are imported. Without this every task reads as "not registered"
        # and the gate would pass by finding nothing to run.
        importlib.import_module(module)

    failures: list[tuple[str, str]] = []
    for entry_name, task_name in interval_tasks(app.conf.beat_schedule):
        task = app.tasks.get(task_name)
        if task is None:
            failures.append((task_name, f"scheduled as {entry_name!r} but not registered"))
            continue
        try:
            result = task()
        # Deliberately broad: the point is to report ANY failure a scheduled task
        # can produce, not to anticipate which kinds.
        except Exception as exc:
            failures.append((task_name, f"{type(exc).__name__}: {exc}"))
            traceback.print_exc()
        else:
            print(f"  ok   {task_name} -> {result}")
    return failures


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "services" / "worker"))
    from app.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    covered = interval_tasks(schedule)
    if not covered:
        # An empty run is not a pass. If nothing was discovered, the gate is measuring
        # nothing and must say so rather than reporting success.
        print(
            "beat smoke: no interval-scheduled tasks found; nothing was verified", file=sys.stderr
        )
        return 1

    print(f"beat smoke: running {len(covered)} interval-scheduled task(s)")
    for name, task_name in skipped_tasks(schedule):
        print(f"  skip {task_name} (scheduled as {name!r} at a fixed time, not an interval)")

    failures = run_smoke(celery_app)
    if failures:
        print("", file=sys.stderr)
        for task_name, reason in failures:
            print(f"beat smoke FAILED: {task_name}: {reason}", file=sys.stderr)
        return 1

    print("beat smoke: all scheduled tasks ran without raising")
    return 0


if __name__ == "__main__":
    sys.exit(main())
