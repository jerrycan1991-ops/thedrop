"""Shared pytest configuration.

Tests that need a live PostgreSQL or Redis are marked and skipped automatically when
those services are unreachable. That keeps the suite runnable before Docker Desktop is
installed, without pretending the skipped tests passed -- pytest reports them as
skipped, with a reason.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The API package is not installed as a distribution during a bare test run.
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

# Tests must never touch a real deployment.
os.environ.setdefault("ENVIRONMENT", "development")


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _postgres_reachable(timeout: float = 4.0) -> bool:
    """Whether db-marked tests can actually run, decided by CONNECTING.

    Two things were wrong with the previous `127.0.0.1:5432` port probe.

    It could not see the database at all once Postgres moved off the box (ADR-0012 puts
    it on a managed provider), so every db-marked test silently skipped. "Skipped" and
    "passed" look far too similar in a summary line.

    Worse, a probe answers "yes" for ANY Postgres listening locally, including one whose
    credentials we do not have -- the normal situation on a developer machine with an
    unrelated Postgres installed. The tests then ran, failed at fixture setup, and
    reported as ERRORs rather than skips: 147 of them on every full run, which trains
    everyone to stop reading the summary.

    Connecting distinguishes "no database" from "a database I cannot use", and both from
    "ready".
    """
    try:
        import psycopg
        from thedrop_config import get_settings
    except ImportError:
        return False

    # The URL the application itself would use: the environment when set, otherwise
    # whatever settings resolves (a .env file, or the packaged default).
    url = os.environ.get("DATABASE_URL", "").strip() or str(get_settings().database_url)

    try:
        with psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://"),
            connect_timeout=int(timeout),
        ):
            return True
    except Exception:
        # Any failure -- unreachable, wrong password, missing database -- means the same
        # thing here: these tests cannot run.
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    postgres_up = _postgres_reachable()
    redis_up = _port_open("127.0.0.1", int(os.environ.get("REDIS_PORT", 6379)))
    api_up = _port_open("127.0.0.1", int(os.environ.get("API_PORT", 8000)))
    web_up = _port_open("127.0.0.1", int(os.environ.get("WEB_PORT", 3100)))

    skip_db = pytest.mark.skip(
        reason="PostgreSQL not reachable. Set DATABASE_URL to a real database (the "
        "managed one is fine), or start a local instance with "
        "docker compose -f infrastructure/docker/docker-compose.dev.yml up -d"
    )
    skip_redis = pytest.mark.skip(reason="Redis not reachable on 127.0.0.1:6379")
    skip_api = pytest.mark.skip(
        reason="FastAPI not reachable on 127.0.0.1:8000 - run `pnpm dev:api`"
    )

    for item in items:
        if "db" in item.keywords and not postgres_up:
            item.add_marker(skip_db)
        if "redis" in item.keywords and not redis_up:
            item.add_marker(skip_redis)
        if "api" in item.keywords and not api_up:
            item.add_marker(skip_api)
        if "web" in item.keywords and not web_up:
            item.add_marker(
                pytest.mark.skip(
                    reason="Next.js not reachable on 127.0.0.1:3100 - run `pnpm dev:web`"
                )
            )
