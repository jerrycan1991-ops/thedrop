"""Redis session lifecycle — regression coverage before the auth migration.

These tests pin behaviour that the API baseline cannot see. The baseline captures
response bodies; it cannot tell you that the idle TTL slid, that an epoch bump
invalidated a live session, or that logout actually removed the Redis key.

Every one of these behaviours has to be reproduced exactly when session validation
moves to Node. The subtle one is the TTL slide: get it wrong and sessions expire two
hours later regardless of activity, which nothing else here would catch.

Requires Postgres, Redis and the FastAPI service. Skipped automatically otherwise —
see tests/conftest.py.

NOTHING in the session implementation is changed by this file. Two tests temporarily
mutate state (session_epoch, a session's TTL) and restore it in a finally block.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis as redis_lib
from sqlalchemy import select
from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.models import User

pytestmark = [pytest.mark.db, pytest.mark.redis, pytest.mark.api, pytest.mark.integration]

SESSION_COOKIE = "thedrop_session"
ME = "/api/v1/admin/auth/me"


def _session_key(session_id: str) -> str:
    return f"session:{session_id}"


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def client(settings) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.api_port}", timeout=15.0) as c:
        yield c


@pytest.fixture(scope="module")
def r(settings) -> Iterator[redis_lib.Redis]:
    conn = redis_lib.from_url(str(settings.redis_url), decode_responses=True)
    yield conn
    conn.close()


@pytest.fixture
def session_id(client: httpx.Client, settings) -> Iterator[str]:
    """A fresh login per test, cleaned up afterwards."""
    if not settings.admin_email or not settings.admin_initial_password:
        pytest.skip("ADMIN_EMAIL / ADMIN_INITIAL_PASSWORD not configured")

    client.cookies.clear()
    response = client.post(
        "/api/v1/admin/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_initial_password,
        },
    )
    assert response.status_code == 200, response.text
    sid = response.cookies.get(SESSION_COOKIE)
    assert sid

    yield sid

    _call(client, "POST", "/api/v1/admin/auth/logout", sid)


def _call(
    client: httpx.Client, method: str, path: str, sid: str | None = None
) -> httpx.Response:
    """Request with an explicit cookie header and no jar.

    httpx.Client keeps a cookie jar, so a client that has logged in stays
    authenticated on every later request — which silently turns an "anonymous"
    assertion into an authenticated one. Clearing the jar and setting the header
    directly makes each call mean exactly what it says.
    """
    client.cookies.clear()
    headers = {"Cookie": f"{SESSION_COOKIE}={sid}"} if sid is not None else {}
    return client.request(method, path, headers=headers)


def _get_me(client: httpx.Client, sid: str | None) -> httpx.Response:
    return _call(client, "GET", ME, sid)


class TestSessionAcceptance:
    def test_valid_session_succeeds(self, client: httpx.Client, session_id: str) -> None:
        response = _get_me(client, session_id)
        assert response.status_code == 200
        assert response.json()["roles"] == ["admin"]

    def test_missing_session_is_401(self, client: httpx.Client) -> None:
        response = _get_me(client, None)
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated"

    def test_unknown_session_is_401(self, client: httpx.Client) -> None:
        # A well-formed but non-existent id: the key simply is not in Redis.
        response = _get_me(client, "definitely-not-a-real-session-identifier")
        assert response.status_code == 401
        assert response.json()["detail"] == "Session expired"

    def test_empty_cookie_is_401(self, client: httpx.Client) -> None:
        # Present but empty: FastAPI treats a falsy cookie as absent.
        response = _get_me(client, "")
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated"


class TestSessionPayloadFormat:
    """The stored shape is a migration contract: Node must read the same keys."""

    def test_redis_key_format(self, r: redis_lib.Redis, session_id: str) -> None:
        assert r.exists(_session_key(session_id)) == 1

    def test_payload_keys(self, r: redis_lib.Redis, session_id: str) -> None:
        payload = json.loads(r.get(_session_key(session_id)))
        assert set(payload) == {
            "user_id",
            "email",
            "roles",
            "epoch",
            "created_at",
            "absolute_expiry",
        }

    def test_payload_types(self, r: redis_lib.Redis, session_id: str) -> None:
        payload = json.loads(r.get(_session_key(session_id)))
        assert isinstance(payload["user_id"], int)
        assert isinstance(payload["roles"], list)
        assert isinstance(payload["epoch"], int)
        # Both timestamps must round-trip through fromisoformat, which is how the
        # application reads them back.
        datetime.fromisoformat(payload["created_at"])
        datetime.fromisoformat(payload["absolute_expiry"])

    def test_absolute_expiry_matches_configured_ttl(
        self, r: redis_lib.Redis, session_id: str, settings
    ) -> None:
        payload = json.loads(r.get(_session_key(session_id)))
        expiry = datetime.fromisoformat(payload["absolute_expiry"])
        expected = datetime.now(UTC) + timedelta(hours=settings.session_absolute_ttl_hours)
        assert abs((expiry - expected).total_seconds()) < 60


class TestIdleTtlSliding:
    def test_initial_ttl_is_the_idle_window(
        self, r: redis_lib.Redis, session_id: str, settings
    ) -> None:
        ttl = r.ttl(_session_key(session_id))
        assert 0 < ttl <= settings.session_idle_ttl_hours * 3600

    def test_authenticated_request_slides_the_ttl(
        self, client: httpx.Client, r: redis_lib.Redis, session_id: str, settings
    ) -> None:
        """The behaviour most likely to be dropped in a rewrite.

        Rather than sleeping, the TTL is pushed down directly and then observed
        recovering — deterministic and instant, but it exercises the same code path.
        """
        key = _session_key(session_id)
        full = settings.session_idle_ttl_hours * 3600

        r.expire(key, 60)
        assert r.ttl(key) <= 60

        assert _get_me(client, session_id).status_code == 200

        slid = r.ttl(key)
        assert slid > 60, "idle TTL was not refreshed by an authenticated request"
        assert slid > full - 60

    def test_anonymous_request_does_not_slide_another_session(
        self, client: httpx.Client, r: redis_lib.Redis, session_id: str
    ) -> None:
        key = _session_key(session_id)
        r.expire(key, 120)
        _get_me(client, None)
        assert r.ttl(key) <= 120


class TestSessionExpiry:
    def test_deleted_key_is_401(
        self, client: httpx.Client, r: redis_lib.Redis, session_id: str
    ) -> None:
        # Exactly what natural TTL expiry leaves behind: no key.
        r.delete(_session_key(session_id))
        response = _get_me(client, session_id)
        assert response.status_code == 401
        assert response.json()["detail"] == "Session expired"

    def test_absolute_expiry_is_enforced_even_with_a_live_key(
        self, client: httpx.Client, r: redis_lib.Redis, session_id: str
    ) -> None:
        """The 12h ceiling is application-level, not a Redis TTL.

        A sliding TTL alone would let an active session live forever, so the payload
        carries its own deadline. This proves the check is real: the key is present
        and unexpired, but the request is still rejected.
        """
        key = _session_key(session_id)
        payload = json.loads(r.get(key))
        payload["absolute_expiry"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        r.set(key, json.dumps(payload), keepttl=True)

        response = _get_me(client, session_id)
        assert response.status_code == 401
        assert response.json()["detail"] == "Session expired"

        # And the stale session is cleaned up rather than left to linger.
        assert r.exists(key) == 0


class TestEpochInvalidation:
    def test_epoch_bump_invalidates_a_live_session(
        self, client: httpx.Client, r: redis_lib.Redis, session_id: str
    ) -> None:
        """Bumping session_epoch is the incident-response kill switch.

        It must invalidate sessions immediately, without waiting for a TTL.
        """
        assert _get_me(client, session_id).status_code == 200

        with session_scope() as db:
            user = db.scalar(select(User).where(User.email.isnot(None)).order_by(User.id))
            assert user is not None
            original = user.session_epoch
            user.session_epoch = original + 1

        try:
            response = _get_me(client, session_id)
            assert response.status_code == 401
            assert response.json()["detail"] == "Session invalidated"
            # The invalidated session is destroyed, not merely rejected.
            assert r.exists(_session_key(session_id)) == 0
        finally:
            with session_scope() as db:
                user = db.scalar(select(User).order_by(User.id))
                assert user is not None
                user.session_epoch = original


class TestLogout:
    def test_logout_removes_the_redis_key(
        self, client: httpx.Client, r: redis_lib.Redis, session_id: str
    ) -> None:
        key = _session_key(session_id)
        assert r.exists(key) == 1

        response = _call(client, "POST", "/api/v1/admin/auth/logout", session_id)
        assert response.status_code == 200
        assert r.exists(key) == 0

    def test_session_is_unusable_after_logout(
        self, client: httpx.Client, session_id: str
    ) -> None:
        _call(client, "POST", "/api/v1/admin/auth/logout", session_id)
        assert _get_me(client, session_id).status_code == 401

    def test_logout_clears_the_cookie(self, client: httpx.Client, session_id: str) -> None:
        response = _call(client, "POST", "/api/v1/admin/auth/logout", session_id)
        set_cookie = response.headers.get("set-cookie", "")
        # Deletion is expressed as an immediate expiry; attributes must match those
        # used when setting it or the browser keeps the original cookie.
        assert SESSION_COOKIE in set_cookie
        assert 'Max-Age=0' in set_cookie or "expires=Thu, 01 Jan 1970" in set_cookie.lower()


class TestRbacBoundary:
    def test_anonymous_cannot_reach_any_admin_route(self, client: httpx.Client) -> None:
        for path in (
            "/api/v1/admin/auth/me",
            "/api/v1/admin/articles",
            "/api/v1/admin/settings",
            "/api/v1/admin/system/metrics",
        ):
            assert _call(client, "GET", path).status_code == 401, path

    def test_admin_role_satisfies_every_requirement(
        self, client: httpx.Client, session_id: str
    ) -> None:
        # `admin` implicitly passes editor/analyst/viewer checks.
        for path in (
            "/api/v1/admin/articles",
            "/api/v1/admin/settings",
            "/api/v1/admin/system/metrics",
        ):
            assert _get_me(client, session_id).status_code == 200
            assert _call(client, "GET", path, session_id).status_code == 200, path
