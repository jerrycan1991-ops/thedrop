"""RBAC matrix, role ordering, and login/lockout behaviour.

The expectations here were CAPTURED from the running FastAPI service, not derived from
what the rules ought to be. Where the observed behaviour is odd it is pinned as-is and
flagged in a docstring; this file records the contract, it does not redesign it.

Requires Postgres, Redis and FastAPI; the parity tests additionally require Next.js.
Fixture accounts are created per module and deleted in teardown.
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

pytestmark = [pytest.mark.db, pytest.mark.redis, pytest.mark.api, pytest.mark.integration]

SESSION_COOKIE = "thedrop_session"

# ---------------------------------------------------------------------------
# Observed FastAPI behaviour, captured 31 August 2026.
#
#   endpoint -> {principal: status}
#
# `anonymous` is every unauthenticated caller. `multi` holds viewer+editor+analyst.
# ---------------------------------------------------------------------------
RBAC_MATRIX: dict[tuple[str, str], dict[str, int]] = {
    ("GET", "/api/v1/admin/auth/me"): {
        "anonymous": 401, "admin": 200, "editor": 200,
        "analyst": 200, "viewer": 200, "multi": 200,
    },
    ("GET", "/api/v1/admin/articles"): {
        "anonymous": 401, "admin": 200, "editor": 200,
        "analyst": 200, "viewer": 200, "multi": 200,
    },
    # editor allowed; analyst and viewer denied.
    ("GET", "/api/v1/admin/settings"): {
        "anonymous": 401, "admin": 200, "editor": 200,
        "analyst": 403, "viewer": 403, "multi": 200,
    },
    # The exact inverse of /settings: editor denied, analyst and viewer allowed.
    # See TestAuthorizationConsistency below.
    ("GET", "/api/v1/admin/system/metrics"): {
        "anonymous": 401, "admin": 200, "editor": 403,
        "analyst": 200, "viewer": 200, "multi": 200,
    },
    # Probed against a non-existent key so authorization is observable without a
    # write: passing authz yields 404, failing it yields 403.
    ("PUT", "/api/v1/admin/settings/zz-does-not-exist"): {
        "anonymous": 401, "admin": 404, "editor": 403,
        "analyst": 403, "viewer": 403, "multi": 403,
    },
}

PRINCIPALS = ("anonymous", "admin", "editor", "analyst", "viewer", "multi")


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def py_client(settings) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.api_port}", timeout=30.0) as c:
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
    # Rate-limit counters outlive the users by 15 minutes and would poison a rerun.
    for key in r.scan_iter("login_attempts:*"):
        if EMAIL_PREFIX in key:
            r.delete(key)
    cleanup_fixture_users()


def _login(client: httpx.Client, email: str, password: str) -> httpx.Response:
    client.cookies.clear()
    return client.post(
        "/api/v1/admin/auth/login", json={"email": email, "password": password}
    )


def _session_for(client: httpx.Client, fixtures: FixtureSet, principal: str) -> str | None:
    if principal == "anonymous":
        return None
    user = fixtures[principal]
    response = _login(client, user.email, user.password)
    assert response.status_code == 200, f"{principal}: {response.text[:200]}"
    return response.cookies.get(SESSION_COOKIE)


def _call(
    client: httpx.Client, method: str, path: str, sid: str | None
) -> httpx.Response:
    client.cookies.clear()
    headers = {"Cookie": f"{SESSION_COOKIE}={sid}"} if sid else {}
    body = {"value": {"value": False}} if method == "PUT" else None
    return client.request(method, path, headers=headers, json=body)


class TestRbacMatrix:
    @pytest.mark.parametrize(("method", "path"), list(RBAC_MATRIX))
    @pytest.mark.parametrize("principal", PRINCIPALS)
    def test_matrix(
        self, py_client, fixtures, principal: str, method: str, path: str
    ) -> None:
        expected = RBAC_MATRIX[(method, path)][principal]
        sid = _session_for(py_client, fixtures, principal)
        actual = _call(py_client, method, path, sid).status_code
        assert actual == expected, (
            f"{principal} {method} {path}: expected {expected}, got {actual}"
        )

    def test_logout_is_available_to_every_authenticated_role(
        self, py_client, fixtures
    ) -> None:
        for principal in PRINCIPALS:
            sid = _session_for(py_client, fixtures, principal)
            response = _call(py_client, "POST", "/api/v1/admin/auth/logout", sid)
            expected = 401 if principal == "anonymous" else 200
            assert response.status_code == expected, principal


class TestAuthorizationConsistency:
    """Documents inconsistencies rather than fixing them.

    These tests PIN the current behaviour. They are expected to pass; each one exists
    so that a future change to the policy is a deliberate, visible decision rather
    than an accident.
    """

    def test_editor_and_analyst_permissions_do_not_nest(
        self, py_client, fixtures
    ) -> None:
        """Neither role is a superset of the other — there is no privilege ladder.

        editor  : /settings YES, /system/metrics NO
        analyst : /settings NO,  /system/metrics YES

        A reader who assumes roles are ordered by privilege will get this wrong.
        """
        editor = _session_for(py_client, fixtures, "editor")
        analyst = _session_for(py_client, fixtures, "analyst")

        assert _call(py_client, "GET", "/api/v1/admin/settings", editor).status_code == 200
        assert (
            _call(py_client, "GET", "/api/v1/admin/system/metrics", editor).status_code == 403
        )
        assert _call(py_client, "GET", "/api/v1/admin/settings", analyst).status_code == 403
        assert (
            _call(py_client, "GET", "/api/v1/admin/system/metrics", analyst).status_code == 200
        )

    def test_viewer_outranks_editor_for_system_metrics(
        self, py_client, fixtures
    ) -> None:
        """`viewer`, the least-privileged role, can read metrics; `editor` cannot.

        Almost certainly unintended: `require_role("analyst", "viewer")` on
        /system/metrics simply omits editor.
        """
        viewer = _session_for(py_client, fixtures, "viewer")
        editor = _session_for(py_client, fixtures, "editor")

        assert (
            _call(py_client, "GET", "/api/v1/admin/system/metrics", viewer).status_code == 200
        )
        assert (
            _call(py_client, "GET", "/api/v1/admin/system/metrics", editor).status_code == 403
        )

    def test_viewer_can_list_unpublished_drafts(self, py_client, fixtures) -> None:
        """`/admin/articles` includes drafts and is open to `viewer`.

        Defensible for an internal tool, but worth stating explicitly: "viewer" does
        not mean "published content only".
        """
        viewer = _session_for(py_client, fixtures, "viewer")
        response = _call(
            py_client, "GET", "/api/v1/admin/articles?status_filter=draft", viewer
        )
        assert response.status_code == 200

    def test_multiple_roles_grant_the_union(self, py_client, fixtures) -> None:
        """Role combination widens access; it never narrows it."""
        multi = _session_for(py_client, fixtures, "multi")
        assert _call(py_client, "GET", "/api/v1/admin/settings", multi).status_code == 200
        assert (
            _call(py_client, "GET", "/api/v1/admin/system/metrics", multi).status_code == 200
        )
        # ...but the union of non-admin roles still does not reach an admin-only route.
        assert (
            _call(
                py_client, "PUT", "/api/v1/admin/settings/zz-does-not-exist", multi
            ).status_code
            == 403
        )


class TestRoleOrdering:
    """Canonical ordering: alphabetical by slug, resolved by the database."""

    def test_multi_role_user_returns_alphabetical_order(
        self, py_client, fixtures
    ) -> None:
        sid = _session_for(py_client, fixtures, "multi")
        response = _call(py_client, "GET", "/api/v1/admin/auth/me", sid)
        assert response.status_code == 200
        # Assigned viewer, editor, analyst — deliberately not alphabetical, so a
        # pass cannot come from insertion order coinciding with the expectation.
        assert response.json()["roles"] == ["analyst", "editor", "viewer"]

    def test_ordering_is_stable_across_repeated_requests(
        self, py_client, fixtures
    ) -> None:
        sid = _session_for(py_client, fixtures, "multi")
        seen = {
            tuple(_call(py_client, "GET", "/api/v1/admin/auth/me", sid).json()["roles"])
            for _ in range(8)
        }
        assert len(seen) == 1, f"ordering is non-deterministic: {seen}"

    def test_ordering_is_stable_across_separate_logins(
        self, py_client, fixtures
    ) -> None:
        seen = set()
        for _ in range(4):
            sid = _session_for(py_client, fixtures, "multi")
            seen.add(
                tuple(_call(py_client, "GET", "/api/v1/admin/auth/me", sid).json()["roles"])
            )
        assert len(seen) == 1, f"ordering varies between sessions: {seen}"

    def test_session_payload_roles_also_ordered(self, py_client, fixtures, r) -> None:
        """The login-time snapshot in Redis uses the same ordering."""
        sid = _session_for(py_client, fixtures, "multi")
        payload = json.loads(r.get(f"session:{sid}"))
        assert payload["roles"] == ["analyst", "editor", "viewer"]


class TestLoginFailureAndLockout:
    """Captured against a fixture account — never the real admin.

    The rate limiter keys on IP + email, so exhausting one fixture account has no
    effect on any other account.
    """

    @pytest.fixture(autouse=True)
    def _clear_counter(self, r, fixtures):
        """Each test starts from a clean counter and an unlocked account."""
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

    def test_wrong_password_is_401(self, py_client, fixtures) -> None:
        response = _login(py_client, fixtures["viewer"].email, "definitely-wrong")
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid email or password"

    def test_unknown_account_is_indistinguishable_from_a_wrong_password(
        self, py_client, fixtures
    ) -> None:
        unknown = _login(py_client, "zz-rbac-nobody@thedrop.channel", "whatever")
        wrong = _login(py_client, fixtures["viewer"].email, "definitely-wrong")
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json(), "response reveals whether the account exists"

    def test_rate_limit_engages_after_five_failures(self, py_client, fixtures) -> None:
        """Observed sequence: five 401s, then 429.

        The counter is read BEFORE the attempt and incremented after, so the
        threshold is crossed on the sixth request.
        """
        email = fixtures["analyst"].email
        statuses = [_login(py_client, email, "wrong").status_code for _ in range(6)]
        assert statuses == [401, 401, 401, 401, 401, 429], statuses

    def test_correct_password_is_also_rate_limited(self, py_client, fixtures) -> None:
        """The limiter runs before the password check, so a locked-out user cannot
        log in even with the right credentials — and receives 429, not 423.

        This means the 423 "account locked" branch is effectively unreachable
        through this endpoint: the Redis counter and `failed_login_count` both hit
        five at the same moment, and the rate limit is checked first. Recorded as
        observed behaviour, not endorsed.
        """
        user = fixtures["editor"]
        for _ in range(6):
            _login(py_client, user.email, "wrong")

        response = _login(py_client, user.email, user.password)
        assert response.status_code == 429
        assert response.json()["detail"] == "Too many attempts. Try again later."

    def test_lockout_is_recorded_in_the_database(self, py_client, fixtures) -> None:
        user = fixtures["viewer"]
        for _ in range(5):
            _login(py_client, user.email, "wrong")

        with session_scope() as db:
            row = db.scalar(select(User).where(User.email == user.email))
            assert row.failed_login_count >= 5
            assert row.locked_until is not None, "account was not locked after 5 failures"

    def test_rate_limit_is_scoped_per_account(self, py_client, fixtures) -> None:
        """Exhausting one account must not lock out another."""
        victim = fixtures["analyst"]
        bystander = fixtures["viewer"]

        for _ in range(6):
            _login(py_client, victim.email, "wrong")

        assert _login(py_client, victim.email, victim.password).status_code == 429
        assert _login(py_client, bystander.email, bystander.password).status_code == 200

    def test_successful_login_resets_the_failure_counter(
        self, py_client, fixtures
    ) -> None:
        user = fixtures["multi"]
        for _ in range(3):
            _login(py_client, user.email, "wrong")

        assert _login(py_client, user.email, user.password).status_code == 200

        with session_scope() as db:
            row = db.scalar(select(User).where(User.email == user.email))
            assert row.failed_login_count == 0
            assert row.locked_until is None
