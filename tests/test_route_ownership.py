"""Route ownership: which tier actually serves each API path.

This exists because ownership was silently wrong once and nothing caught it.
`GET /api/v1/public/articles/{category}/{year}/{month}/{day}/{slug}` had a Next.js
route handler on disk and in the build manifest, yet a catch-all rewrite
(`/api/v1/:path*`) beat it and FastAPI served the route in both dev and a production
build. Every parity test passed, because both tiers returned identical responses.

Two layers of protection:

  1. A STATIC check that needs no running services: every Next.js route handler on
     disk must be un-shadowed by a proxy rule, and the catch-all must not come back.
     This is the regression guard and it runs everywhere, including CI.

  2. A LIVE check that proves ownership against the running dev server using a
     controlled difference rather than response equality.

Ownership can only be proven by a difference. Identical responses prove nothing, which
is precisely how the original defect survived.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from thedrop_config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_APP = REPO_ROOT / "apps" / "web"
NEXT_CONFIG = WEB_APP / "next.config.ts"
API_DIR = WEB_APP / "app" / "api"

#: Paths FastAPI still owns. Kept here as the expected proxy set so that migrating an
#: endpoint requires updating this list deliberately.
EXPECTED_PROXIED = {
    "/api/v1/worker/:path*",
    "/api/v1/admin/settings/:key",
}


def _node_route_paths() -> list[str]:
    """Every Next.js route handler on disk, as a URL path with its dynamic segments."""
    paths = []
    for route in API_DIR.rglob("route.ts"):
        rel = route.relative_to(WEB_APP / "app").parent.as_posix()
        paths.append("/" + rel)
    return sorted(paths)


def _rewrite_sources() -> list[str]:
    """`source:` values from the rewrites() block of next.config.ts."""
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    start = text.index("async rewrites()")
    end = text.index("async headers()")
    block = text[start:end]
    # Ignore commented-out examples: the rollback snippet lives in a comment.
    live = "\n".join(
        line for line in block.splitlines() if not line.strip().startswith(("*", "//", "/*"))
    )
    return re.findall(r'source:\s*"([^"]+)"', live)


class TestStaticOwnership:
    """No running services required. This is the guard that must never be deleted."""

    def test_route_handlers_exist(self) -> None:
        paths = _node_route_paths()
        assert len(paths) == 10, f"expected 10 Node route handlers, found {len(paths)}: {paths}"

    def test_no_catch_all_rewrite(self) -> None:
        """A catch-all silently steals deeply-nested dynamic route handlers.

        `/api/v1/:path*` matched the five-segment article route ahead of the handler,
        so FastAPI served an endpoint everyone believed had been migrated.
        """
        sources = _rewrite_sources()
        for source in sources:
            assert source != "/api/v1/:path*", (
                "the catch-all rewrite is back; it shadows nested Node route handlers"
            )
            assert not re.fullmatch(r"/api(/v\d+)?/:path\*", source), (
                f"rewrite {source!r} is a catch-all over the API surface"
            )

    def test_proxy_list_is_exactly_the_fastapi_owned_paths(self) -> None:
        assert set(_rewrite_sources()) == EXPECTED_PROXIED

    def test_no_proxy_rule_shadows_a_node_route(self) -> None:
        """Convert each rewrite source to a regex and assert no Node path matches it."""

        def to_regex(source: str) -> re.Pattern[str]:
            pattern = re.escape(source)
            pattern = pattern.replace(r"/:path\*", "(/.*)?")
            pattern = re.sub(r"\\:[a-zA-Z_]+", "[^/]+", pattern)
            return re.compile(f"^{pattern}$")

        proxies = [(s, to_regex(s)) for s in _rewrite_sources()]

        for node_path in _node_route_paths():
            # A Next dynamic segment matches one path segment, same as a rewrite param.
            concrete = re.sub(r"\[[^\]]+\]", "x", node_path)
            for source, rx in proxies:
                assert not rx.match(concrete), (
                    f"rewrite {source!r} shadows Node route {node_path!r} "
                    f"(concrete form {concrete!r})"
                )

    def test_settings_collection_is_not_shadowed_by_the_key_rule(self) -> None:
        """`/admin/settings` (Node) vs `/admin/settings/{key}` (FastAPI).

        `:key` must match exactly one segment. If it were `:key*` the collection route
        would be swallowed and admin settings would silently move back to FastAPI.
        """
        sources = _rewrite_sources()
        assert "/api/v1/admin/settings/:key" in sources
        assert "/api/v1/admin/settings/:key*" not in sources
        assert "/api/v1/admin/settings" not in sources


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    settings = get_settings()
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.web_port}", timeout=30.0) as c:
        yield c


@pytest.mark.web
@pytest.mark.integration
class TestLiveOwnership:
    """Proves ownership against the running dev server via a controlled difference.

    Next.js App Router route handlers emit an RSC negotiation `vary` header
    (`rsc, next-router-state-tree, ...`). A proxied response does not. This is a
    development-server signal: a standalone production build does not emit it, so the
    production equivalent of this check is the DATABASE_URL probe described in
    docs/API_BASELINE.md rather than this header.
    """

    @staticmethod
    def _is_node(response: httpx.Response) -> bool:
        return "rsc" in (response.headers.get("vary") or "").lower()

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/public/categories"),
            ("GET", "/api/v1/public/articles"),
            ("GET", "/api/v1/public/latest"),
            ("GET", "/api/v1/public/articles/trending/2026/01/01/nope"),
            ("GET", "/api/v1/admin/auth/me"),
            ("GET", "/api/v1/admin/articles"),
            ("GET", "/api/v1/admin/settings"),
            ("GET", "/api/v1/admin/system/metrics"),
            ("POST", "/api/v1/admin/auth/login"),
            ("POST", "/api/v1/admin/auth/logout"),
        ],
    )
    def test_node_owned(self, client: httpx.Client, method: str, path: str) -> None:
        response = client.request(method, path, json={} if method == "POST" else None)
        assert self._is_node(response), f"{method} {path} is NOT served by Node"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/worker/status"),
            ("POST", "/api/v1/worker/heartbeat"),
            ("POST", "/api/v1/worker/jobs/claim"),
            ("POST", "/api/v1/worker/jobs/abc/complete"),
            ("POST", "/api/v1/worker/jobs/abc/fail"),
            ("PUT", "/api/v1/admin/settings/zz-probe"),
        ],
    )
    def test_fastapi_owned(self, client: httpx.Client, method: str, path: str) -> None:
        response = client.request(method, path, json={})
        assert not self._is_node(response), f"{method} {path} is unexpectedly served by Node"

    @pytest.mark.parametrize("path", ["/healthz", "/readyz"])
    def test_health_endpoints_are_not_proxied(self, client: httpx.Client, path: str) -> None:
        """Deliberate: they report the FastAPI process's own health.

        Anything monitoring them must target the API port directly, not the web port.
        """
        assert client.get(path).status_code == 404
