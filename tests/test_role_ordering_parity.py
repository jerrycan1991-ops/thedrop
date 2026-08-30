"""Role-ordering parity between FastAPI and Node for a multi-role user.

Phase 3C could not verify role ordering: the only account held a single role, so the
array order was unobservable. With a multi-role fixture it becomes a real contract,
and this is the file that proves both implementations agree on it.

Canonical ordering is alphabetical by slug, resolved by PostgreSQL on both sides —
Python via `order_by="Role.slug"` on the relationship, Node via `ORDER BY r.slug`.
Sorting in application code would risk Python's codepoint order diverging from the
database collation.

Requires Postgres, Redis, FastAPI and Next.js.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import redis as redis_lib
from rbac_fixtures import (
    FixtureSet,
    cleanup_fixture_sessions,
    cleanup_fixture_users,
    create_fixture_users,
)
from thedrop_config import get_settings

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    pytest.mark.api,
    pytest.mark.web,
    pytest.mark.integration,
]

SESSION_COOKIE = "thedrop_session"
ME = "/api/v1/admin/auth/me"


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
    cleanup_fixture_users()


def _login(client: httpx.Client, email: str, password: str) -> str:
    client.cookies.clear()
    response = client.post(
        "/api/v1/admin/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    sid = response.cookies.get(SESSION_COOKIE)
    assert sid
    client.cookies.clear()
    return sid


def _me(client: httpx.Client, sid: str) -> httpx.Response:
    client.cookies.clear()
    return client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={sid}"})


class TestMultiRoleParity:
    def test_both_return_identical_bodies(self, py_client, node_client, fixtures) -> None:
        """Sessions are always created by FastAPI; Node only validates them."""
        sid = _login(py_client, fixtures["multi"].email, fixtures["multi"].password)

        py = _me(py_client, sid)
        node = _me(node_client, sid)

        assert py.status_code == node.status_code == 200
        assert py.json() == node.json()

    def test_both_return_the_canonical_order(self, py_client, node_client, fixtures) -> None:
        user = fixtures["multi"]
        sid = _login(py_client, user.email, user.password)

        expected = ["analyst", "editor", "viewer"]
        # Roles were assigned viewer, editor, analyst -- deliberately not alphabetical,
        # so neither implementation can pass by echoing insertion order.
        assert user.expected_roles == expected

        assert _me(py_client, sid).json()["roles"] == expected
        assert _me(node_client, sid).json()["roles"] == expected

    def test_order_is_deterministic_across_repeated_requests(
        self, py_client, node_client, fixtures
    ) -> None:
        sid = _login(py_client, fixtures["multi"].email, fixtures["multi"].password)

        py_seen = {tuple(_me(py_client, sid).json()["roles"]) for _ in range(6)}
        node_seen = {tuple(_me(node_client, sid).json()["roles"]) for _ in range(6)}

        assert len(py_seen) == 1, f"python ordering varies: {py_seen}"
        assert len(node_seen) == 1, f"node ordering varies: {node_seen}"
        assert py_seen == node_seen

    def test_order_is_deterministic_across_separate_sessions(
        self, py_client, node_client, fixtures
    ) -> None:
        user = fixtures["multi"]
        seen = set()
        for _ in range(3):
            sid = _login(py_client, user.email, user.password)
            seen.add(tuple(_me(py_client, sid).json()["roles"]))
            seen.add(tuple(_me(node_client, sid).json()["roles"]))
        assert len(seen) == 1, f"ordering varies between sessions: {seen}"

    @pytest.mark.parametrize("principal", ["admin", "editor", "analyst", "viewer"])
    def test_single_role_users_also_match(
        self, py_client, node_client, fixtures, principal: str
    ) -> None:
        user = fixtures[principal]
        sid = _login(py_client, user.email, user.password)

        py, node = _me(py_client, sid), _me(node_client, sid)
        assert py.json() == node.json()
        assert py.json()["roles"] == user.expected_roles

    def test_node_reads_roles_from_the_database_not_the_session_payload(
        self, py_client, node_client, fixtures, r
    ) -> None:
        """A revoked role must take effect on the next request, not at next login.

        The payload written at login is a snapshot; both implementations re-read
        `user_roles`. Tampering with the snapshot must therefore change nothing.
        """
        import json

        user = fixtures["multi"]
        sid = _login(py_client, user.email, user.password)
        key = f"session:{sid}"

        payload = json.loads(r.get(key))
        payload["roles"] = ["totally", "wrong", "values"]
        r.set(key, json.dumps(payload), keepttl=True)

        expected = ["analyst", "editor", "viewer"]
        assert _me(py_client, sid).json()["roles"] == expected
        assert _me(node_client, sid).json()["roles"] == expected
