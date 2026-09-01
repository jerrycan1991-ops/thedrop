"""Regression tests for the API baseline harness itself.

The baseline tool is the compatibility authority for the Node migration, so a defect
in the harness is worse than a defect in a single endpoint: it silently withdraws
verification from whatever it touches.

The defect these tests pin: `capture()` and `compare()` log in once up front through a
shared `httpx.Client`. httpx keeps a persistent cookie jar per client, and
`client.request(..., cookies=None)` merges with that jar rather than suppressing it.
So every endpoint registered with `needs_session=False` -- the four `*_anonymous`
admin entries whose whole purpose is to pin the 401 -- was sent WITH a valid admin
session and answered 200. A full run reported four false failures, and the anonymous
guard was no longer verified by any full run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL_PATH = REPO_ROOT / "infrastructure" / "scripts" / "api_baseline.py"


def _load_tool() -> Any:
    """Load api_baseline.py by path -- infrastructure/scripts is not a package."""
    spec = importlib.util.spec_from_file_location("api_baseline", _TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The endpoints that exist purely to assert the guard. If any of these ever answers
#: 200, the harness is authenticating requests it is supposed to leave anonymous.
ANONYMOUS_ADMIN_PATHS = [
    "/api/v1/admin/auth/me",
    "/api/v1/admin/articles",
    "/api/v1/admin/settings",
    "/api/v1/admin/system/metrics",
]

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(scope="module")
def tool() -> Any:
    return _load_tool()


@pytest.mark.api
def test_anonymous_endpoints_stay_anonymous_in_a_run_that_logs_in(tool: Any) -> None:
    """The regression itself.

    Mirrors `compare()`: one client, log in first (as it must, for the authenticated
    endpoints), then fetch the anonymous ones. All four must still be rejected.
    """
    with httpx.Client(base_url=BASE_URL, timeout=20.0, follow_redirects=False) as client:
        session_id = tool.login(client)
        try:
            for path in ANONYMOUS_ADMIN_PATHS:
                record = tool.fetch(client, "GET", path, None)
                assert record["status"] == 401, (
                    f"{path} returned {record['status']} for an anonymous capture. "
                    "The harness is leaking a session -- most likely through the "
                    "httpx cookie jar. See the module docstring."
                )
        finally:
            tool._logout(client, session_id)


@pytest.mark.api
def test_authenticated_endpoints_still_receive_the_session(tool: Any) -> None:
    """The other half, and the reason the test above is not sufficient alone.

    A 'fix' that simply stopped attaching the session would make every anonymous check
    pass while quietly withdrawing verification from every authenticated endpoint. This
    asserts the session still reaches the requests that are supposed to carry one.
    """
    with httpx.Client(base_url=BASE_URL, timeout=20.0, follow_redirects=False) as client:
        session_id = tool.login(client)
        try:
            record = tool.fetch(client, "GET", "/api/v1/admin/auth/me", session_id)
            assert record["status"] == 200, (
                f"authenticated /auth/me returned {record['status']}; the harness is no "
                "longer attaching the session to requests that need one."
            )
        finally:
            tool._logout(client, session_id)


@pytest.mark.api
def test_login_leaves_no_session_in_the_client_cookie_jar(tool: Any) -> None:
    """Pin the mechanism, not just the symptom.

    The two tests above would still pass if someone reintroduced the jar and papered
    over it elsewhere. This asserts the root cause stays fixed: after `login`, the
    client carries no session of its own.
    """
    with httpx.Client(base_url=BASE_URL, timeout=20.0, follow_redirects=False) as client:
        session_id = tool.login(client)
        try:
            assert client.cookies.get("thedrop_session") is None, (
                "login left the session in the client's cookie jar; every subsequent "
                "anonymous request in the same run would be authenticated."
            )
        finally:
            tool._logout(client, session_id)
