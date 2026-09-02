"""Queue a job for the desktop runner to claim.

Run on the VPS, where the database credentials live:

    python -m thedrop_database.enqueue_job --type noop
    python -m thedrop_database.enqueue_job --type noop --payload '{"sleep_seconds": 5}'
    python -m thedrop_database.enqueue_job --type embed --priority 10

Exists mostly for verification: enqueue a `noop`, watch the runner claim, execute and
complete it, and the desktop-VPS contract is proven end to end without any provider,
model or GPU being involved. Scheduled work is enqueued by the Celery tasks in
services/worker, not by hand.

The runner is only ever leased job types it advertised, so enqueuing a type no runner
handles leaves the row sitting `queued` rather than failing -- which is the intended
behaviour, not a bug: the handler may simply not be deployed yet.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys

from thedrop_database import session_scope
from thedrop_database.models import Job

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("enqueue-job")


def enqueue(job_type: str, payload: dict, priority: int, key: str | None) -> int:
    # idempotency_key is NOT NULL and unique: it is what makes a job that finished
    # exactly as its lease expired safe to re-run. A random one is right for manual
    # enqueues; real producers derive it from the work itself so a duplicate enqueue
    # collapses into one row.
    idempotency_key = key or f"manual-{secrets.token_hex(8)}"

    with session_scope() as db:
        db.add(
            Job(
                job_type=job_type,
                payload=payload,
                priority=priority,
                idempotency_key=idempotency_key,
            )
        )

    logger.info("queued %s (priority %d, key %s)", job_type, priority, idempotency_key)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", required=True, help="Job type, e.g. noop")
    parser.add_argument("--payload", default="{}", help="JSON object passed to the handler")
    parser.add_argument(
        "--priority", type=int, default=0, help="Higher is claimed first. Default 0."
    )
    parser.add_argument(
        "--key",
        default=None,
        help="Idempotency key. Defaults to a random one; reuse to make enqueuing a no-op.",
    )
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        logger.error("--payload is not valid JSON: %s", exc)
        return 2
    if not isinstance(payload, dict):
        logger.error("--payload must be a JSON object, got %s", type(payload).__name__)
        return 2

    return enqueue(args.type, payload, args.priority, args.key)


if __name__ == "__main__":
    sys.exit(main())
