"""Shared FastAPI dependencies: database, Redis, sessions and RBAC.

Every admin route must depend on ``require_role``. A test enumerates the router table
and fails if any ``/admin`` route lacks an authorization dependency -- the check that
catches the endpoint someone forgot to protect.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import redis
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from thedrop_config import Settings, get_settings
from thedrop_database import get_session
from thedrop_database.models import User, WorkerNode

from app.security import constant_time_equals, token_digest

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = redis.from_url(
            str(settings.redis_url), decode_responses=True, socket_timeout=3
        )
    return _redis_client


RedisDep = Annotated[redis.Redis, Depends(get_redis)]


# ----------------------------------------------------------------- admin sessions


def _session_key(session_id: str) -> str:
    return f"session:{session_id}"


def create_session(r: redis.Redis, user: User, settings: Settings, session_id: str) -> None:
    """Server-side session record. The cookie carries only an opaque id."""
    now = datetime.now(UTC)
    payload = {
        "user_id": user.id,
        "email": user.email,
        "roles": [role.slug for role in user.roles],
        "epoch": user.session_epoch,
        "created_at": now.isoformat(),
        "absolute_expiry": (
            now + timedelta(hours=settings.session_absolute_ttl_hours)
        ).isoformat(),
    }
    r.setex(
        _session_key(session_id),
        timedelta(hours=settings.session_idle_ttl_hours),
        json.dumps(payload),
    )


def destroy_session(r: redis.Redis, session_id: str) -> None:
    r.delete(_session_key(session_id))


def get_current_user(
    db: SessionDep,
    r: RedisDep,
    settings: SettingsDep,
    session_cookie: Annotated[str | None, Cookie(alias="thedrop_session")] = None,
) -> User:
    if not session_cookie:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    raw = r.get(_session_key(session_cookie))
    if raw is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    payload: dict[str, Any] = json.loads(raw)

    # Absolute lifetime is independent of activity, so a busy session cannot live forever.
    if datetime.fromisoformat(payload["absolute_expiry"]) < datetime.now(UTC):
        destroy_session(r, session_cookie)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    user = db.scalar(
        select(User).options(selectinload(User.roles)).where(User.id == payload["user_id"])
    )
    if user is None or not user.is_active:
        destroy_session(r, session_cookie)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account unavailable")

    # Bumping session_epoch invalidates every session for the user at once -- password
    # change, role change, incident response.
    if user.session_epoch != payload.get("epoch"):
        destroy_session(r, session_cookie)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session invalidated")

    # Sliding idle window.
    r.expire(_session_key(session_cookie), timedelta(hours=settings.session_idle_ttl_hours))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*allowed: str):
    """Authorization dependency. ``admin`` implicitly satisfies every requirement."""

    def _check(user: CurrentUser) -> User:
        slugs = {role.slug for role in user.roles}
        if "admin" in slugs or slugs.intersection(allowed):
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")

    return _check


# ----------------------------------------------------------------- worker auth


def get_worker_node(request: Request, db: SessionDep) -> WorkerNode:
    """Authenticate the desktop AI worker.

    Bearer token, compared against a stored SHA-256 digest in constant time. During a
    rotation window the previous token also validates, so rotating is not an outage.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing worker token")

    digest = token_digest(header.split(" ", 1)[1].strip())
    now = datetime.now(UTC)

    for node in db.scalars(select(WorkerNode).where(WorkerNode.is_active.is_(True))):
        if constant_time_equals(node.token_hash, digest):
            return node
        if (
            node.previous_token_hash is not None
            and node.rotation_grace_until is not None
            and node.rotation_grace_until > now
            and constant_time_equals(node.previous_token_hash, digest)
        ):
            return node

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid worker token")


WorkerDep = Annotated[WorkerNode, Depends(get_worker_node)]


def db_session() -> Generator[Session, None, None]:
    yield from get_session()
