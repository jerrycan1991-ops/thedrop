"""Register a desktop worker node and mint its token.

Run on the VPS, where the database credentials live:

    python -m thedrop_database.provision_worker --name desktop-4070
    python -m thedrop_database.provision_worker --name desktop-4070 --rotate

The token is generated here, never chosen by the operator, and **printed exactly
once**. Only its SHA-256 digest is stored, so a database leak yields no working
credential and a lost token can only be replaced, not recovered.

``--rotate`` issues a new token while the previous one keeps working for a grace
window, so rotating is not an outage: start the runner with the new token, confirm it
heartbeats, and the old one expires on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import secrets
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from thedrop_database import session_scope
from thedrop_database.models import WorkerNode

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("provision-worker")

#: Long enough that guessing is not a threat model; URL-safe so it survives being
#: pasted into an env file, a shell, or a systemd unit without quoting surprises.
TOKEN_BYTES = 32

#: How long the previous token keeps working after a rotation. Long enough to notice a
#: runner that failed to pick up the new one, short enough that a leaked token is not
#: valid indefinitely.
ROTATION_GRACE = timedelta(hours=24)

#: What this node advertises it can execute. The API only leases matching job types, so
#: a runner is never handed work it cannot do. Handlers are added as phases land; the
#: skeleton ships with the no-op only.
DEFAULT_CAPABILITIES = {
    "gpu": True,
    "vram_gb": 12,
    "handlers": ["noop"],
}


def _digest(token: str) -> bytes:
    """Must match `token_digest` in services/api/app/security.py."""
    return hashlib.sha256(token.encode()).digest()


def provision(name: str, rotate: bool) -> int:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = datetime.now(UTC)

    with session_scope() as db:
        node = db.scalar(select(WorkerNode).where(WorkerNode.name == name))

        if node is None:
            if rotate:
                logger.error("no worker named %r exists; run without --rotate first", name)
                return 1
            db.add(
                WorkerNode(
                    name=name,
                    token_hash=_digest(token),
                    capabilities=DEFAULT_CAPABILITIES,
                    is_active=True,
                )
            )
            action = "created"
        elif rotate:
            # Keep the old digest valid for the grace window. The API checks it second,
            # so a runner mid-request during rotation is not cut off.
            node.previous_token_hash = node.token_hash
            node.rotation_grace_until = now + ROTATION_GRACE
            node.token_hash = _digest(token)
            node.token_rotated_at = now
            node.is_active = True
            action = "rotated"
        else:
            logger.error(
                "worker %r already exists. Use --rotate to issue a new token; the "
                "existing one keeps working for %d hours.",
                name,
                int(ROTATION_GRACE.total_seconds() // 3600),
            )
            return 1

    # Printed once, outside the transaction, and never logged anywhere else.
    print()
    print(f"worker {action}: {name}")
    print()
    print("WORKER_TOKEN is shown ONCE. Store it in the runner's env file now:")
    print()
    print(f"  WORKER_TOKEN={token}")
    print()
    if action == "rotated":
        print(
            f"  The previous token stays valid until "
            f"{(now + ROTATION_GRACE).isoformat(timespec='seconds')}."
        )
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Worker node name, e.g. desktop-4070")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Issue a new token for an existing worker, keeping the old one valid briefly.",
    )
    args = parser.parse_args()
    return provision(args.name, args.rotate)


if __name__ == "__main__":
    sys.exit(main())
