"""Python vs Node parity for GET /auth/me — the Phase 3C compatibility authority.

FastAPI (:8000) still creates every session; Next.js (:3100) only validates them. These
tests prove the two implementations are indistinguishable to a client, and — just as
importantly — that they leave Redis in the same state.

Comparing JSON alone is not enough. Each rejection path is checked for its exact
`detail` string AND for whether the Redis key survives, because "returns 401" and
"returns 401 and destroys the session" are very different security properties.

Side-effect tests use a FRESH session per implementation: the first call may delete the
key, so reusing one would make the second assertion meaningless.

Requires Postgres, Redis, FastAPI and Next.js. Skipped automatically otherwise.
Nothing in the session implementation is modified; `session_epoch` is restored in a
finally block.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import redis as redis_lib
from sqlalchemy import select
from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.models import User

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    pytest.mark.api,
    pytest.mark.web,
    pytest.mark.integration,
]

SESSION_COOKIE = "thedrop_session"
ME = "/api/v1/admin/auth/me"

#: Compared on every call. Cache-Control matters most: an admin identity response that
#: becomes cacheable is a cross-user data leak, not a performance regression.
#:
#: `vary` is deliberately EXCLUDED. Next.js appends
#: `vary: rsc, next-router-state-tree, ...` to every App Router response as part of its
#: RSC negotiation. It is framework-emitted, cannot be suppressed without fighting the
#: router, and is inert here because these responses carry no Cache-Control. Excluding
#: it is a scoped, documented exception -- not a licence to ignore header differences.
COMPARED_HEADERS = ("content-type", "cache-control", "www-authenticate")


def _key(sid: str) -> str:
    return f"session:{sid}"


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def py_client(settings) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.api_port}", timeout=30.0) as c:
        yield c


@pytest.fixture(scope="module")
def node_client(settings) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.web_port}", timeout=30.0) as c:
        yield c


@pytest.fixture(scope="module")
def r(settings) -> Iterator[redis_lib.Redis]:
    conn = redis_lib.from_url(str(settings.redis_url), decode_responses=True)
    yield conn
    conn.close()


def _login(client: httpx.Client, settings) -> str:
    """Always against FastAPI — Python remains the only session creator in this phase."""
    client.cookies.clear()
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_initial_password},
    )
    assert response.status_code == 200, response.text
    sid = response.cookies.get(SESSION_COOKIE)
    assert sid
    client.cookies.clear()
    return sid


@pytest.fixture
def new_session(py_client: httpx.Client, settings, r: redis_lib.Redis):
    """Factory: a fresh Python-created session per call, cleaned up afterwards.

    Without the teardown every test would leave live sessions in Redis for the full
    two-hour idle window.
    """
    if not settings.admin_email or not settings.admin_initial_password:
        pytest.skip("ADMIN_EMAIL / ADMIN_INITIAL_PASSWORD not configured")

    created: list[str] = []

    def _make() -> str:
        sid = _login(py_client, settings)
        created.append(sid)
        return sid

    yield _make

    for sid in created:
        r.delete(_key(sid))


def _call(client: httpx.Client, sid: str | None) -> httpx.Response:
    """Explicit cookie header, no jar — see tests/test_session_lifecycle.py."""
    client.cookies.clear()
    headers = {"Cookie": f"{SESSION_COOKIE}={sid}"} if sid is not None else {}
    return client.get(ME, headers=headers)


def _snapshot(response: httpx.Response) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "json": response.json(),
        "headers": {h: response.headers.get(h) for h in COMPARED_HEADERS},
    }


def _assert_identical(py: httpx.Response, node: httpx.Response) -> None:
    a, b = _snapshot(py), _snapshot(node)
    assert a["status"] == b["status"], f"status: python={a['status']} node={b['status']}"
    assert a["json"] == b["json"], f"body: python={a['json']} node={b['json']}"
    assert a["headers"] == b["headers"], f"headers: python={a['headers']} node={b['headers']}"


class TestValidSession:
    def test_authenticated_response_is_identical(
        self, py_client, node_client, new_session
    ) -> None:
        sid = new_session()
        _assert_identical(_call(py_client, sid), _call(node_client, sid))

    def test_node_returns_the_expected_contract(self, node_client, new_session) -> None:
        response = _call(node_client, new_session())
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"id", "email", "displayName", "roles", "mfaEnabled"}
        assert body["roles"] == ["admin"]
        assert body["mfaEnabled"] is False

    def test_neither_sets_cache_control(self, py_client, node_client, new_session) -> None:
        """Both agree: no Cache-Control on this route.

        Recorded as parity, not as approval. Neither implementation sends
        `no-store`, and a 200 with no cache directives is eligible for HEURISTIC
        caching by an intermediary. Nothing caches it today (Vercel does not cache
        force-dynamic routes, and the response is uncacheable in practice), but both
        implementations should send `Cache-Control: no-store` on authenticated
        responses. Fixing it means changing FastAPI, which is out of scope for 3C.
        """
        sid = new_session()
        assert _call(py_client, sid).headers.get("cache-control") is None
        assert _call(node_client, sid).headers.get("cache-control") is None

    def test_vary_difference_is_the_only_header_divergence(
        self, py_client, node_client, new_session
    ) -> None:
        """Pins the exception: nothing else may differ, now or later."""
        sid = new_session()
        py, node = _call(py_client, sid), _call(node_client, sid)

        ignored = {"vary", "date", "server", "connection", "keep-alive",
                   "content-length", "transfer-encoding", "x-request-id"}
        py_headers = {k.lower(): v for k, v in py.headers.items() if k.lower() not in ignored}
        node_headers = {k.lower(): v for k, v in node.headers.items() if k.lower() not in ignored}

        assert py_headers == node_headers, (
            f"unexpected header divergence: python={py_headers} node={node_headers}"
        )


class TestRejectionPaths:
    """Each path: identical response AND identical Redis side effect."""

    def test_missing_cookie(self, py_client, node_client) -> None:
        py, node = _call(py_client, None), _call(node_client, None)
        _assert_identical(py, node)
        assert py.status_code == 401
        assert py.json()["detail"] == "Not authenticated"

    def test_empty_cookie(self, py_client, node_client) -> None:
        py, node = _call(py_client, ""), _call(node_client, "")
        _assert_identical(py, node)
        assert py.json()["detail"] == "Not authenticated"

    def test_unknown_session_id(self, py_client, node_client) -> None:
        sid = "this-session-id-does-not-exist-anywhere"
        py, node = _call(py_client, sid), _call(node_client, sid)
        _assert_identical(py, node)
        assert py.json()["detail"] == "Session expired"

    def test_deleted_key(self, py_client, node_client, new_session, r) -> None:
        for client in (py_client, node_client):
            sid = new_session()
            r.delete(_key(sid))
            response = _call(client, sid)
            assert response.status_code == 401
            assert response.json()["detail"] == "Session expired"

    def test_absolute_expiry_deletes_the_key_in_both(
        self, py_client, node_client, new_session, r
    ) -> None:
        """The key is live in Redis but the payload deadline has passed.

        Both implementations must reject AND clean up — a rejected-but-surviving
        session would be resurrected by any later TTL refresh.
        """
        for client in (py_client, node_client):
            sid = new_session()
            key = _key(sid)

            payload = json.loads(r.get(key))
            payload["absolute_expiry"] = (
                datetime.now(UTC) - timedelta(minutes=1)
            ).isoformat()
            r.set(key, json.dumps(payload), keepttl=True)

            response = _call(client, sid)
            assert response.status_code == 401
            assert response.json()["detail"] == "Session expired"
            assert r.exists(key) == 0, "expired session was not destroyed"

    def test_epoch_mismatch_deletes_the_key_in_both(
        self, py_client, node_client, new_session, r
    ) -> None:
        """The incident-response kill switch must work identically in both."""
        with session_scope() as db:
            user = db.scalar(select(User).order_by(User.id))
            assert user is not None
            original = user.session_epoch

        try:
            for client in (py_client, node_client):
                sid = new_session()
                key = _key(sid)
                assert r.exists(key) == 1

                with session_scope() as db:
                    user = db.scalar(select(User).order_by(User.id))
                    user.session_epoch = user.session_epoch + 1

                response = _call(client, sid)
                assert response.status_code == 401
                assert response.json()["detail"] == "Session invalidated"
                assert r.exists(key) == 0, "invalidated session was not destroyed"
        finally:
            with session_scope() as db:
                user = db.scalar(select(User).order_by(User.id))
                user.session_epoch = original


class TestRedisSideEffects:
    def test_both_slide_the_idle_ttl_identically(
        self, py_client, node_client, new_session, r, settings
    ) -> None:
        """The behaviour most likely to be silently dropped in a port."""
        full = settings.session_idle_ttl_hours * 3600
        observed = {}

        for name, client in (("python", py_client), ("node", node_client)):
            sid = new_session()
            key = _key(sid)

            r.expire(key, 60)
            assert r.ttl(key) <= 60

            assert _call(client, sid).status_code == 200

            slid = r.ttl(key)
            assert slid > 60, f"{name} did not refresh the idle TTL"
            observed[name] = slid

        # Both must land on the same window, not merely "some larger number".
        assert abs(observed["python"] - observed["node"]) <= 5
        assert observed["node"] > full - 60

    def test_neither_slides_ttl_on_an_anonymous_request(
        self, py_client, node_client, new_session, r
    ) -> None:
        sid = new_session()
        key = _key(sid)
        r.expire(key, 120)

        _call(py_client, None)
        _call(node_client, None)

        assert r.ttl(key) <= 120

    def test_rejected_request_does_not_slide_ttl(
        self, py_client, node_client, new_session, r
    ) -> None:
        # An unknown session must not touch an unrelated live session's TTL.
        sid = new_session()
        key = _key(sid)
        r.expire(key, 120)

        _call(py_client, "some-other-unknown-session")
        _call(node_client, "some-other-unknown-session")

        assert r.ttl(key) <= 120

    def test_valid_request_does_not_mutate_the_payload(
        self, node_client, new_session, r
    ) -> None:
        """Only the TTL changes. The stored payload is read-only to the validator."""
        sid = new_session()
        key = _key(sid)
        before = json.loads(r.get(key))

        assert _call(node_client, sid).status_code == 200

        assert json.loads(r.get(key)) == before


class TestSecurityBoundary:
    def test_node_does_not_accept_a_forged_session(self, node_client) -> None:
        for forged in ("../admin", "session:admin", "'; DROP TABLE users;--", "null", "0"):
            response = _call(node_client, forged)
            assert response.status_code == 401, forged

    def test_node_never_reflects_the_session_id(self, node_client, new_session) -> None:
        sid = new_session()
        response = _call(node_client, sid)
        assert sid not in response.text
        assert sid not in json.dumps(dict(response.headers))

    def test_fastapi_implementation_still_serves(self, py_client, new_session) -> None:
        """Rollback safety: the Python route must remain live and correct."""
        assert _call(py_client, new_session()).status_code == 200
