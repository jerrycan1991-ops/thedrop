"""Admin API.

Every route here requires an authenticated session. ``test_all_admin_routes_are_guarded``
walks the router and fails if any path under this prefix lacks an authorization
dependency -- so a forgotten guard is a test failure, not an incident.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from thedrop_database.enums import ArticleStatus, JobStatus, WorkerStatus
from thedrop_database.models import Article, AuditLog, Job, Setting, User, WorkerNode

from app.deps import (
    CurrentUser,
    RedisDep,
    SessionDep,
    SettingsDep,
    create_session,
    destroy_session,
    require_role,
)
from app.security import hash_password, needs_rehash, new_session_id, verify_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 900
_LOCKOUT_MINUTES = 15


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


def _audit(
    db: SessionDep,
    request: Request,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_type="user" if actor_id else "system",
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    )


# ----------------------------------------------------------------------- auth


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: SessionDep,
    r: RedisDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"login_attempts:{client_ip}:{payload.email.lower()}"

    attempts = int(r.get(rate_key) or 0)
    if attempts >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again later."
        )

    user = db.scalar(
        select(User).options(selectinload(User.roles)).where(User.email == payload.email.lower())
    )

    # One generic failure message and one code path, so timing and wording do not
    # reveal whether the account exists.
    def _fail() -> None:
        pipe = r.pipeline()
        pipe.incr(rate_key)
        pipe.expire(rate_key, _LOGIN_WINDOW_SECONDS)
        pipe.execute()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    if user is None or not user.is_active:
        _fail()
    assert user is not None  # narrowed by _fail raising

    if user.locked_until and user.locked_until > datetime.now(UTC):
        raise HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked")

    if not verify_password(user.password_hash, payload.password):
        user.failed_login_count += 1
        if user.failed_login_count >= _LOGIN_MAX_ATTEMPTS:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=_LOCKOUT_MINUTES)
        _audit(db, request, action="login.failed", entity_type="user", entity_id=str(user.id))
        db.commit()
        _fail()

    # Transparent upgrade if the argon2 parameters have since been raised.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)

    session_id = new_session_id()
    create_session(r, user, settings, session_id)

    _audit(db, request, action="login.success", entity_type="user", entity_id=str(user.id),
           actor_id=str(user.id))
    db.commit()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.session_absolute_ttl_hours * 3600,
        path="/",
    )
    return {
        "user": {
            "id": str(user.public_id),
            "email": user.email,
            "displayName": user.display_name,
            "roles": [role.slug for role in user.roles],
        }
    }


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    r: RedisDep,
    settings: SettingsDep,
    user: CurrentUser,
) -> dict[str, str]:
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        destroy_session(r, session_id)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"status": "ok"}


@router.get("/auth/me")
def me(user: CurrentUser) -> dict[str, Any]:
    return {
        "id": str(user.public_id),
        "email": user.email,
        "displayName": user.display_name,
        "roles": [role.slug for role in user.roles],
        "mfaEnabled": user.mfa_enabled,
    }


# ------------------------------------------------------------------- articles


@router.get("/articles", dependencies=[Depends(require_role("editor", "analyst", "viewer"))])
def list_articles(
    db: SessionDep,
    status_filter: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    query = select(Article).options(selectinload(Article.category)).where(
        Article.deleted_at.is_(None)
    )
    if status_filter:
        query = query.where(Article.status == status_filter)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(Article.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return {
        "items": [
            {
                "id": str(a.public_id),
                "headline": a.headline,
                "slug": a.slug,
                "status": a.status,
                "articleType": a.article_type,
                "category": a.category.slug,
                "riskTier": a.risk_tier,
                "editorialConfidence": a.editorial_confidence,
                "publishedAt": a.published_at.isoformat() if a.published_at else None,
                "createdAt": a.created_at.isoformat(),
            }
            for a in rows
        ],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


# --------------------------------------------------------------------- system


@router.get("/system/metrics", dependencies=[Depends(require_role("analyst", "viewer"))])
def system_metrics(db: SessionDep, r: RedisDep) -> dict[str, Any]:
    """Live operational picture. Replaces a Prometheus stack we cannot afford to run."""
    now = datetime.now(UTC)

    article_counts = dict(
        db.execute(
            select(Article.status, func.count())
            .where(Article.deleted_at.is_(None))
            .group_by(Article.status)
        ).all()
    )
    job_counts = dict(
        db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    )

    oldest_queued = db.scalar(
        select(func.min(Job.created_at)).where(Job.status == JobStatus.QUEUED)
    )

    workers = db.scalars(select(WorkerNode).where(WorkerNode.is_active.is_(True))).all()
    worker_payload = []
    for node in workers:
        # Two missed heartbeats marks a node offline. The site does not care -- jobs
        # simply queue until the desktop returns.
        stale = (
            node.last_heartbeat_at is None
            or (now - node.last_heartbeat_at) > timedelta(seconds=90)
        )
        worker_payload.append(
            {
                "name": node.name,
                "status": WorkerStatus.OFFLINE if stale else node.status,
                "lastHeartbeatAt": (
                    node.last_heartbeat_at.isoformat() if node.last_heartbeat_at else None
                ),
                "currentJobCount": node.current_job_count,
                "gpuName": node.gpu_name,
                "gpuVramFreeMb": node.gpu_vram_free_mb,
                "agentVersion": node.agent_version,
            }
        )

    redis_ok = True
    try:
        r.ping()
    except Exception:
        redis_ok = False

    return {
        "generatedAt": now.isoformat(),
        "articles": {
            "byStatus": article_counts,
            "publishedToday": db.scalar(
                select(func.count())
                .select_from(Article)
                .where(
                    Article.status == ArticleStatus.PUBLISHED,
                    Article.published_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
                )
            )
            or 0,
        },
        "jobs": {
            "byStatus": job_counts,
            "queueDepth": job_counts.get(JobStatus.QUEUED, 0),
            "oldestQueuedJobAgeSeconds": (
                int((now - oldest_queued).total_seconds()) if oldest_queued else None
            ),
        },
        "workers": worker_payload,
        "redis": redis_ok,
    }


@router.get("/settings", dependencies=[Depends(require_role("editor"))])
def list_settings(db: SessionDep) -> list[dict[str, Any]]:
    rows = db.scalars(select(Setting).order_by(Setting.key)).all()
    return [
        {
            "key": s.key,
            "value": s.value,
            "description": s.description,
            "isProtected": s.is_protected,
        }
        for s in rows
    ]


class SettingUpdate(BaseModel):
    value: dict[str, Any]


@router.put("/settings/{key}")
def update_setting(
    key: str,
    payload: SettingUpdate,
    request: Request,
    db: SessionDep,
    user: Annotated[User, Depends(require_role("admin"))],
) -> dict[str, Any]:
    setting = db.scalar(select(Setting).where(Setting.key == key))
    if setting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Setting not found")

    # Protected settings are the verification, security and audit controls. They are
    # changeable by a human admin through this route, but never by the self-improvement
    # framework, which has no session and cannot reach it (SECURITY.md §11).
    before = dict(setting.value)
    setting.value = payload.value

    _audit(
        db,
        request,
        action="setting.updated",
        entity_type="setting",
        entity_id=key,
        actor_id=str(user.id),
        before=before,
        after=payload.value,
    )
    db.commit()
    return {"key": setting.key, "value": setting.value}
