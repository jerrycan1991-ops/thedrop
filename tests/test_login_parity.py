"""Python vs Node parity for POST /auth/login.

The most security-sensitive port in the migration. Every branch is compared on both
servers: validation, anti-enumeration, rate limiting, lockout, counter resets, the
Redis session payload, and the cookie attributes.

Two things this proves beyond simple response equality:

  * A session minted by Node is accepted by FastAPI and vice versa. The session format
    is a shared contract, not two implementations that happen to agree today.
  * The rate-limit counter is SHARED between the tiers (same Redis key: ip + email),
    so attempts against one server count against the other. Tested explicitly, because
    it is easy to assume otherwise and it matters during a gradual cutover.

Fixture accounts only — the real admin is never locked.
Requires Postgres, Redis, FastAPI and Next.js.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import redis as redis_lib
from rbac_fixtures import (
    EMAIL_PREFIX,
    FixtureSet,
    cleanup_fixture_sessions,
    cleanup_fixture_users,
    create_fixture_users,
)
from sqlalchemy import select, update
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

LOGIN = "/api/v1/admin/auth/login"
LOGOUT = "/api/v1/admin/auth/logout"
ME = "/api/v1/admin/auth/me"
SESSION_COOKIE = "thedrop_session"


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


@pytest.fixture(scope="module")
def fixtures(r: redis_lib.Redis) -> Iterator[FixtureSet]:
    created = create_fixture_users()
    yield created
    cleanup_fixture_sessions(r)
    for key in r.scan_iter("login_attempts:*"):
        if EMAIL_PREFIX in key:
            r.delete(key)
    cleanup_fixture_users()


@pytest.fixture(autouse=True)
def _reset(r: redis_lib.Redis, fixtures: FixtureSet):
    """Clean counters and unlock accounts around every test.

    The rate-limit key is shared between the two servers, so without this a test that
    exhausts an account on one tier would poison the next test on the other.
    """

    def reset() -> None:
        for key in r.scan_iter("login_attempts:*"):
            if EMAIL_PREFIX in key:
                r.delete(key)
        with session_scope() as db:
            db.execute(
                update(User)
                .where(User.email.like(f"{EMAIL_PREFIX}%"))
                .values(failed_login_count=0, locked_until=None)
            )

    reset()
    yield
    reset()


def _post(client: httpx.Client, body: dict | str) -> httpx.Response:
    client.cookies.clear()
    if isinstance(body, str):
        return client.post(LOGIN, content=body, headers={"Content-Type": "application/json"})
    return client.post(LOGIN, json=body)


def _assert_same(py: httpx.Response, node: httpx.Response, label: str) -> None:
    assert py.status_code == node.status_code, (
        f"{label}: python={py.status_code} node={node.status_code}"
    )
    py_body, node_body = py.json(), node.json()
    for body in (py_body, node_body):
        body.pop("requestId", None)
    assert py_body == node_body, f"{label}: python={py_body} node={node_body}"
    for header in ("content-type", "cache-control", "x-frame-options"):
        assert py.headers.get(header) == node.headers.get(header), f"{label}: {header}"


class TestValidationParity:
    @pytest.mark.parametrize(
        "body",
        [
            {"email": "not-an-email", "password": "x"},
            {"email": "a@b.local", "password": "x"},
            {"email": "a@b.test", "password": "x"},
            {"email": "a@b.invalid", "password": "x"},
            {"email": "a@localhost", "password": "x"},
            {"email": "@example.com", "password": "x"},
            {"email": "a@", "password": "x"},
            {"email": "a@b", "password": "x"},
            {"email": "a@b..com", "password": "x"},
            {"email": "a@example.com"},
            {"password": "x"},
            {},
            {"email": "a@example.com", "password": ""},
            {"email": "a@example.com", "password": "y" * 257},
        ],
    )
    def test_identical_validation_errors(self, py_client, node_client, body) -> None:
        """Enumerates malformed inputs to MEASURE where the ports diverge.

        Reproducing every email-validator rule in TypeScript is not realistic; this
        pins the cases the endpoint actually meets. A failure here is a real divergence
        to document, not a test to relax.
        """
        _assert_same(_post(py_client, body), _post(node_client, body), str(body)[:60])

    def test_both_reject_a_malformed_json_body(self, py_client, node_client) -> None:
        py, node = _post(py_client, "{not json"), _post(node_client, "{not json")
        assert py.status_code == node.status_code == 422


class TestCredentialParity:
    def test_correct_login_returns_the_same_body(self, py_client, node_client, fixtures) -> None:
        user = fixtures["multi"]
        creds = {"email": user.email, "password": user.password}

        py, node = _post(py_client, creds), _post(node_client, creds)
        assert py.status_code == node.status_code == 200
        assert py.json() == node.json()
        assert py.json()["user"]["roles"] == ["analyst", "editor", "viewer"]

    def test_wrong_password_is_identical(self, py_client, node_client, fixtures) -> None:
        creds = {"email": fixtures["viewer"].email, "password": "wrong"}
        _assert_same(_post(py_client, creds), _post(node_client, creds), "wrong password")

    def test_unknown_account_is_identical(self, py_client, node_client) -> None:
        creds = {"email": "zz-rbac-nobody@thedrop.channel", "password": "wrong"}
        _assert_same(_post(py_client, creds), _post(node_client, creds), "unknown account")

    def test_enumeration_resistance_holds_in_both(
        self, py_client, node_client, fixtures
    ) -> None:
        for client in (py_client, node_client):
            unknown = _post(client, {"email": "zz-rbac-ghost@thedrop.channel", "password": "x"})
            wrong = _post(client, {"email": fixtures["viewer"].email, "password": "x"})
            assert unknown.status_code == wrong.status_code == 401
            assert unknown.json() == wrong.json()

    def test_email_is_matched_case_insensitively_in_both(
        self, py_client, node_client, fixtures
    ) -> None:
        user = fixtures["editor"]
        creds = {"email": user.email.upper(), "password": user.password}
        for client in (py_client, node_client):
            assert _post(client, creds).status_code == 200

    def test_inactive_account_is_rejected_identically(
        self, py_client, node_client, fixtures, r
    ) -> None:
        user = fixtures["analyst"]
        with session_scope() as db:
            db.execute(
                update(User).where(User.email == user.email).values(is_active=False)
            )
        try:
            creds = {"email": user.email, "password": user.password}
            for client in (py_client, node_client):
                for key in r.scan_iter("login_attempts:*"):
                    if EMAIL_PREFIX in key:
                        r.delete(key)
                response = _post(client, creds)
                assert response.status_code == 401
                assert response.json()["detail"] == "Invalid email or password"
        finally:
            with session_scope() as db:
                db.execute(
                    update(User).where(User.email == user.email).values(is_active=True)
                )


class TestRateLimitParity:
    def test_sequence_is_identical(self, py_client, node_client, fixtures, r) -> None:
        expected = [401, 401, 401, 401, 401, 429]

        for name, client in (("python", py_client), ("node", node_client)):
            for key in r.scan_iter("login_attempts:*"):
                if EMAIL_PREFIX in key:
                    r.delete(key)
            with session_scope() as db:
                db.execute(
                    update(User)
                    .where(User.email.like(f"{EMAIL_PREFIX}%"))
                    .values(failed_login_count=0, locked_until=None)
                )

            email = fixtures["viewer"].email
            actual = [
                _post(client, {"email": email, "password": "no"}).status_code
                for _ in range(6)
            ]
            assert actual == expected, f"{name}: {actual}"

    def test_rate_limit_counter_is_shared_between_the_tiers(
        self, py_client, node_client, fixtures, r
    ) -> None:
        """One Redis key per (ip, email), so the limit is global, not per-server.

        This is correct for a gradual cutover — a client cannot get ten attempts by
        alternating servers — but it is the kind of coupling worth proving rather
        than assuming.
        """
        email = fixtures["editor"].email
        for i in range(3):
            assert _post(py_client, {"email": email, "password": "no"}).status_code == 401, i
        for i in range(2):
            assert _post(node_client, {"email": email, "password": "no"}).status_code == 401, i

        # Six attempts total across both servers.
        assert _post(node_client, {"email": email, "password": "no"}).status_code == 429
        assert _post(py_client, {"email": email, "password": "no"}).status_code == 429

    def test_rate_limited_correct_password_is_429_not_423_in_both(
        self, py_client, node_client, fixtures, r
    ) -> None:
        """The documented unreachable-423 behaviour, preserved on both sides."""
        for name, client in (("python", py_client), ("node", node_client)):
            for key in r.scan_iter("login_attempts:*"):
                if EMAIL_PREFIX in key:
                    r.delete(key)
            with session_scope() as db:
                db.execute(
                    update(User)
                    .where(User.email.like(f"{EMAIL_PREFIX}%"))
                    .values(failed_login_count=0, locked_until=None)
                )

            user = fixtures["multi"]
            for _ in range(6):
                _post(client, {"email": user.email, "password": "no"})

            response = _post(client, {"email": user.email, "password": user.password})
            assert response.status_code == 429, name
            assert response.json()["detail"] == "Too many attempts. Try again later."

    def test_lockout_is_recorded_by_both(self, py_client, node_client, fixtures, r) -> None:
        for name, client in (("python", py_client), ("node", node_client)):
            for key in r.scan_iter("login_attempts:*"):
                if EMAIL_PREFIX in key:
                    r.delete(key)
            with session_scope() as db:
                db.execute(
                    update(User)
                    .where(User.email.like(f"{EMAIL_PREFIX}%"))
                    .values(failed_login_count=0, locked_until=None)
                )

            user = fixtures["viewer"]
            for _ in range(5):
                _post(client, {"email": user.email, "password": "no"})

            with session_scope() as db:
                row = db.scalar(select(User).where(User.email == user.email))
                assert row.failed_login_count >= 5, name
                assert row.locked_until is not None, f"{name} did not lock the account"

    def test_successful_login_resets_counters_in_both(
        self, py_client, node_client, fixtures, r
    ) -> None:
        for name, client in (("python", py_client), ("node", node_client)):
            for key in r.scan_iter("login_attempts:*"):
                if EMAIL_PREFIX in key:
                    r.delete(key)
            with session_scope() as db:
                db.execute(
                    update(User)
                    .where(User.email.like(f"{EMAIL_PREFIX}%"))
                    .values(failed_login_count=0, locked_until=None)
                )

            user = fixtures["admin"]
            for _ in range(3):
                _post(client, {"email": user.email, "password": "no"})
            ok = _post(client, {"email": user.email, "password": user.password})
            assert ok.status_code == 200

            with session_scope() as db:
                row = db.scalar(select(User).where(User.email == user.email))
                assert row.failed_login_count == 0, name
                assert row.locked_until is None, name


class TestCookieParity:
    def test_cookie_attributes_match(self, py_client, node_client, fixtures) -> None:
        user = fixtures["admin"]
        creds = {"email": user.email, "password": user.password}

        def attrs(response: httpx.Response) -> dict[str, object]:
            raw = response.headers.get("set-cookie", "")
            lowered = raw.lower()
            out: dict[str, object] = {
                "httponly": "httponly" in lowered,
                "secure": "secure" in lowered,
                "name": raw.split("=", 1)[0].strip(),
            }
            for attr in ("samesite", "path", "max-age", "domain"):
                marker = f"{attr}="
                if marker in lowered:
                    start = lowered.index(marker) + len(marker)
                    end = lowered.find(";", start)
                    out[attr] = raw[start : end if end != -1 else len(raw)].strip().lower()
                else:
                    out[attr] = None
            return out

        py_attrs = attrs(_post(py_client, creds))
        node_attrs = attrs(_post(node_client, creds))
        assert py_attrs == node_attrs, f"python={py_attrs} node={node_attrs}"
        assert py_attrs["httponly"] is True
        assert py_attrs["samesite"] == "lax"
        assert py_attrs["path"] == "/"
        assert py_attrs["max-age"] == "43200"


class TestSessionPayloadParity:
    def test_node_writes_the_same_payload_shape(self, py_client, node_client, fixtures, r) -> None:
        user = fixtures["multi"]
        creds = {"email": user.email, "password": user.password}

        py_sid = _post(py_client, creds).cookies.get(SESSION_COOKIE)
        node_sid = _post(node_client, creds).cookies.get(SESSION_COOKIE)

        py_payload = json.loads(r.get(f"session:{py_sid}"))
        node_payload = json.loads(r.get(f"session:{node_sid}"))

        assert set(py_payload) == set(node_payload)
        for field in ("user_id", "email", "roles", "epoch"):
            assert py_payload[field] == node_payload[field], field

    def test_node_sets_the_same_idle_ttl(
        self, py_client, node_client, fixtures, r, settings
    ) -> None:
        user = fixtures["admin"]
        creds = {"email": user.email, "password": user.password}
        full = settings.session_idle_ttl_hours * 3600

        py_ttl = r.ttl(f"session:{_post(py_client, creds).cookies.get(SESSION_COOKIE)}")
        node_ttl = r.ttl(f"session:{_post(node_client, creds).cookies.get(SESSION_COOKIE)}")

        assert abs(py_ttl - node_ttl) <= 5
        assert node_ttl > full - 60

    def test_session_id_entropy_matches(self, py_client, node_client, fixtures) -> None:
        """secrets.token_urlsafe(32) yields 43 base64url characters."""
        user = fixtures["admin"]
        creds = {"email": user.email, "password": user.password}
        py_sid = _post(py_client, creds).cookies.get(SESSION_COOKIE)
        node_sid = _post(node_client, creds).cookies.get(SESSION_COOKIE)
        assert len(py_sid) == len(node_sid) == 43
        assert "=" not in node_sid


class TestCrossTierSessionCompatibility:
    """The migration's real safety property: either tier accepts the other's session."""

    def test_node_session_is_accepted_by_fastapi(self, py_client, node_client, fixtures) -> None:
        user = fixtures["multi"]
        sid = _post(node_client, {"email": user.email, "password": user.password}).cookies.get(
            SESSION_COOKIE
        )
        py_client.cookies.clear()
        response = py_client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={sid}"})
        assert response.status_code == 200
        assert response.json()["roles"] == ["analyst", "editor", "viewer"]

    def test_fastapi_session_is_accepted_by_node(self, py_client, node_client, fixtures) -> None:
        user = fixtures["multi"]
        sid = _post(py_client, {"email": user.email, "password": user.password}).cookies.get(
            SESSION_COOKIE
        )
        node_client.cookies.clear()
        response = node_client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={sid}"})
        assert response.status_code == 200
        assert response.json()["roles"] == ["analyst", "editor", "viewer"]


class TestAuditTrail:
    def test_both_write_login_audit_rows(self, py_client, node_client, fixtures) -> None:
        from sqlalchemy import func, text

        def count(action: str) -> int:
            with session_scope() as db:
                return db.scalar(
                    select(func.count()).select_from(text("audit_logs")).where(
                        text("action = :a").bindparams(a=action)
                    )
                ) or 0

        user = fixtures["editor"]
        for client in (py_client, node_client):
            before_ok, before_fail = count("login.success"), count("login.failed")
            _post(client, {"email": user.email, "password": "no"})
            _post(client, {"email": user.email, "password": user.password})
            assert count("login.failed") == before_fail + 1
            assert count("login.success") == before_ok + 1


class TestLogoutParity:
    """POST /auth/logout — Redis deletion, cookie clearing, and the 401 path."""

    def _login_on(self, client: httpx.Client, user) -> str:
        sid = _post(client, {"email": user.email, "password": user.password}).cookies.get(
            SESSION_COOKIE
        )
        assert sid
        return sid

    def _logout(self, client: httpx.Client, sid: str | None) -> httpx.Response:
        client.cookies.clear()
        headers = {"Cookie": f"{SESSION_COOKIE}={sid}"} if sid else {}
        return client.post(LOGOUT, headers=headers)

    def test_anonymous_logout_is_identical(self, py_client, node_client) -> None:
        _assert_same(self._logout(py_client, None), self._logout(node_client, None), "anon logout")

    def test_invalid_session_logout_is_identical(self, py_client, node_client) -> None:
        sid = "definitely-not-a-session"
        _assert_same(
            self._logout(py_client, sid), self._logout(node_client, sid), "bad session logout"
        )

    def test_successful_logout_body_is_identical(self, py_client, node_client, fixtures) -> None:
        user = fixtures["admin"]
        py = self._logout(py_client, self._login_on(py_client, user))
        node = self._logout(node_client, self._login_on(node_client, user))

        assert py.status_code == node.status_code == 200
        assert py.json() == node.json() == {"status": "ok"}

    def test_both_delete_the_redis_key(self, py_client, node_client, fixtures, r) -> None:
        user = fixtures["editor"]
        for name, client in (("python", py_client), ("node", node_client)):
            sid = self._login_on(client, user)
            key = f"session:{sid}"
            assert r.exists(key) == 1, name

            assert self._logout(client, sid).status_code == 200
            assert r.exists(key) == 0, f"{name} did not destroy the session"

    def test_session_is_unusable_after_logout_in_both(
        self, py_client, node_client, fixtures
    ) -> None:
        user = fixtures["viewer"]
        for client in (py_client, node_client):
            sid = self._login_on(client, user)
            self._logout(client, sid)
            client.cookies.clear()
            response = client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={sid}"})
            assert response.status_code == 401

    def test_logout_cookie_attributes_match(self, py_client, node_client, fixtures) -> None:
        """Deletion must use the same path/domain as creation, or the browser keeps
        the original cookie and the user appears half-logged-in."""
        user = fixtures["admin"]

        def clearing(response: httpx.Response) -> dict[str, object]:
            raw = response.headers.get("set-cookie", "")
            lowered = raw.lower()
            out: dict[str, object] = {
                "name": raw.split("=", 1)[0].strip(),
                "expired": "max-age=0" in lowered or "01 jan 1970" in lowered,
            }
            for attr in ("path", "domain"):
                marker = f"{attr}="
                if marker in lowered:
                    start = lowered.index(marker) + len(marker)
                    end = lowered.find(";", start)
                    out[attr] = raw[start : end if end != -1 else len(raw)].strip().lower()
                else:
                    out[attr] = None
            return out

        py = clearing(self._logout(py_client, self._login_on(py_client, user)))
        node = clearing(self._logout(node_client, self._login_on(node_client, user)))

        assert py == node, f"python={py} node={node}"
        assert py["expired"] is True
        assert py["path"] == "/"

    def test_logout_writes_no_audit_row_in_either(
        self, py_client, node_client, fixtures
    ) -> None:
        """A documented gap (SECURITY.md §9): login is audited, logout is not.

        Pinned so that implementing it later is a deliberate change on BOTH tiers.
        """
        from sqlalchemy import func, text

        def total() -> int:
            with session_scope() as db:
                return db.scalar(select(func.count()).select_from(text("audit_logs"))) or 0

        user = fixtures["analyst"]
        for client in (py_client, node_client):
            sid = self._login_on(client, user)
            before = total()
            self._logout(client, sid)
            assert total() == before, "logout unexpectedly wrote an audit row"

    def test_node_can_log_out_a_fastapi_session_and_vice_versa(
        self, py_client, node_client, fixtures, r
    ) -> None:
        """Cross-tier: either server can terminate the other's session."""
        user = fixtures["multi"]

        sid = self._login_on(py_client, user)
        assert self._logout(node_client, sid).status_code == 200
        assert r.exists(f"session:{sid}") == 0

        sid = self._login_on(node_client, user)
        assert self._logout(py_client, sid).status_code == 200
        assert r.exists(f"session:{sid}") == 0
