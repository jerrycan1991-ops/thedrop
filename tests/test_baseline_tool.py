"""The baseline tool must not send a session on requests that are meant to be anonymous.

`infrastructure/scripts/api_baseline.py` captures four admin endpoints twice: once
anonymously, pinning the 401 guard, and once with a real session, pinning the response
body. The anonymous half was silently not being tested.

`login()` performed its POST through the shared `httpx.Client`, so the
`Set-Cookie: thedrop_session=...` landed in that client's persistent jar. `fetch()`
then passed `cookies=None` for anonymous endpoints -- which does NOT suppress the jar
in httpx, it merges with it. Every `*_anonymous` endpoint was therefore sent WITH a
valid admin session whenever the same run also selected an authenticated endpoint,
i.e. on every full run. `--group admin` looked fine, because no login happened.

Two consequences, the second the serious one:

  1. A full-run `compare` reported four endpoints differing -- a false failure that
     reads exactly like an auth regression.
  2. The 401 guard those four baselines exist to pin was never actually verified.

These tests run against an in-process `httpx.MockTransport` that enforces the same rule
the real API does -- a session cookie means 200, its absence means 401 -- so they need
no live services and fail loudly if the session ever leaks back onto an anonymous
request.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import thedrop_config

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "infrastructure" / "scripts" / "api_baseline.py"
COMMITTED_BASELINES = REPO_ROOT / "tests" / "baseline"

#: The four endpoints whose entire reason for existing is to pin the anonymous 401.
ANONYMOUS_ADMIN = (
    "admin_me_anonymous",
    "admin_articles_anonymous",
    "admin_settings_anonymous",
    "admin_metrics_anonymous",
)

#: Not a credential -- the value the in-process API mints and the tests look for.
SESSION_VALUE = "regression-test-session"


def _load_tool() -> ModuleType:
    """Import the script by path; `infrastructure/scripts` is not an importable package."""
    spec = importlib.util.spec_from_file_location("api_baseline_under_test", TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApi:
    """A stand-in API that authenticates the way the real one does.

    Recording every request is the point: asserting on responses alone would let a
    future regression hide behind a mock more permissive than production.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/v1/admin/auth/login":
            return httpx.Response(
                200,
                json={"user": {"publicId": "u_test", "email": "admin@example.test"}},
                headers={
                    "set-cookie": (
                        f"thedrop_session={SESSION_VALUE}; HttpOnly; Path=/; SameSite=lax"
                    )
                },
            )
        if path == "/api/v1/admin/auth/logout":
            return httpx.Response(200, json={"ok": True})

        if path.startswith("/api/v1/admin"):
            if SESSION_VALUE in request.headers.get("cookie", ""):
                # A body that could never be mistaken for the pinned 401.
                return httpx.Response(200, json={"items": [], "authenticated": True})
            return httpx.Response(401, json={"detail": "Not authenticated"})

        return httpx.Response(200, json={})

    def cookie_header_for(self, path: str) -> str | None:
        """The Cookie header sent on the LAST request to `path`, if any."""
        for request in reversed(self.requests):
            if request.url.path == path:
                return request.headers.get("cookie")
        return None


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The baseline tool, wired to stub credentials so `login` can run offline."""
    module = _load_tool()

    monkeypatch.setattr(
        thedrop_config,
        "get_settings",
        lambda: SimpleNamespace(
            admin_email="admin@example.test",
            admin_initial_password="stub-password",
        ),
    )
    return module


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    """Route every client the tool builds through the in-process API."""
    api = FakeApi()
    real_client = httpx.Client

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(api.handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", client_factory)
    return api


@pytest.fixture
def anonymous_baselines(tmp_path: Path) -> Path:
    """The four committed anonymous baselines, isolated from the rest of the set.

    The real files are used rather than synthesised ones, so this also proves that what
    is checked into `tests/baseline` is what a full run honours. Endpoints with no
    baseline here are reported as SKIP by `compare`, which is not a failure.
    """
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    for name in ANONYMOUS_ADMIN:
        shutil.copyfile(COMMITTED_BASELINES / f"{name}.json", baseline_dir / f"{name}.json")
    return baseline_dir


def test_committed_anonymous_baselines_pin_the_401(anonymous_baselines: Path) -> None:
    """Guards the guard: the rest of this module means nothing if these drift to 200."""
    for name in ANONYMOUS_ADMIN:
        record = json.loads((anonymous_baselines / f"{name}.json").read_text(encoding="utf-8"))
        assert record["status"] == 401, f"{name} baseline no longer pins the 401"
        assert record["body"] == {"detail": "Not authenticated"}


def test_full_run_compare_still_reports_401_for_anonymous_admin_endpoints(
    tool: ModuleType,
    fake_api: FakeApi,
    anonymous_baselines: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A full run logs in, so this is exactly the case the cookie jar used to poison."""
    monkeypatch.setattr(tool, "BASELINE_DIR", anonymous_baselines)

    exit_code = tool.compare("http://127.0.0.1:8000", None)
    output = capsys.readouterr().out

    # The run really did authenticate -- otherwise this proves nothing, which is how
    # `--group admin` kept passing while the full run failed.
    assert any(r.url.path == "/api/v1/admin/auth/login" for r in fake_api.requests)

    for name in ANONYMOUS_ADMIN:
        assert f"match   {name:38s} 401" in output, f"{name} did not match its 401 baseline"
    assert "DIFF" not in output
    assert exit_code == 0


def test_anonymous_requests_carry_no_cookie_header(
    tool: ModuleType,
    fake_api: FakeApi,
    anonymous_baselines: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire-level statement of the same rule, independent of any baseline file.

    `compare` fetches only endpoints that have a baseline, so the four anonymous admin
    endpoints are the only fetches here -- and none of them may carry a session.
    """
    monkeypatch.setattr(tool, "BASELINE_DIR", anonymous_baselines)
    tool.compare("http://127.0.0.1:8000", None)

    auth_paths = {"/api/v1/admin/auth/login", "/api/v1/admin/auth/logout"}
    fetched = [r for r in fake_api.requests if r.url.path not in auth_paths]

    assert len(fetched) == len(ANONYMOUS_ADMIN)
    for request in fetched:
        cookie = request.headers.get("cookie", "")
        assert SESSION_VALUE not in cookie, f"anonymous {request.url.path} carried a session"


def test_authenticated_fetch_still_sends_the_session(
    tool: ModuleType, fake_api: FakeApi
) -> None:
    """The other half of the contract.

    A "fix" that simply never attaches a cookie would satisfy every assertion above and
    silently turn the authenticated baselines into a second set of 401s.
    """
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5.0) as client:
        session_id = tool.login(client)
        record = tool.fetch(client, "GET", "/api/v1/admin/articles", session_id)

    assert record["status"] == 200
    assert SESSION_VALUE in (fake_api.cookie_header_for("/api/v1/admin/articles") or "")


def test_login_leaves_no_session_in_the_client_cookie_jar(
    tool: ModuleType, fake_api: FakeApi
) -> None:
    """The root cause, pinned directly.

    `httpx.Client` merges its jar into every request it builds, so a session left there
    is attached to calls that never asked for one.
    """
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5.0) as client:
        session_id = tool.login(client)

        assert session_id == SESSION_VALUE
        assert client.cookies.get("thedrop_session") is None
        assert len(client.cookies.jar) == 0
