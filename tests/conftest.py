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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    postgres_up = _port_open("127.0.0.1", int(os.environ.get("POSTGRES_PORT", 5432)))
    redis_up = _port_open("127.0.0.1", int(os.environ.get("REDIS_PORT", 6379)))
    api_up = _port_open("127.0.0.1", int(os.environ.get("API_PORT", 8000)))

    skip_db = pytest.mark.skip(
        reason="PostgreSQL not reachable on 127.0.0.1:5432 - start Docker Desktop, then "
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
