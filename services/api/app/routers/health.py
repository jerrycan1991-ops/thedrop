"""Liveness, readiness and operational metrics.

``/healthz`` answers "is the process up". ``/readyz`` answers "can it actually serve"
-- database reachable, Redis reachable, migrations at head. The distinction matters:
a process that is up but pointing at a database mid-migration must not receive traffic.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app import __version__
from app.deps import RedisDep, SessionDep, SettingsDep

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(settings: SettingsDep) -> dict[str, str]:
    return {
        "status": "ok",
        "version": __version__,
        "environment": settings.environment.value,
    }


@router.get("/readyz")
def readyz(
    response: Response, db: SessionDep, r: RedisDep, settings: SettingsDep
) -> dict[str, Any]:
    database_ok = False
    redis_ok = False
    migrations = "unknown"

    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        logger.exception("readiness: database unreachable")

    if database_ok:
        try:
            current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            migrations = "head" if current else "unknown"
        except Exception:
            # Table absent means migrations have never run.
            migrations = "behind"

    try:
        r.ping()
        redis_ok = True
    except Exception:
        logger.exception("readiness: redis unreachable")

    ready = database_ok and redis_ok and migrations == "head"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if ready else "degraded",
        "version": __version__,
        "environment": settings.environment.value,
        "database": database_ok,
        "redis": redis_ok,
        "migrations": migrations,
    }
