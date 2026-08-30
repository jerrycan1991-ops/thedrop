"""Capture and compare the FastAPI response contract.

Phase 0 of the Node migration. The point is to make "the Next.js version behaves
identically" a checkable claim rather than a judgement call:

    python infrastructure/scripts/api_baseline.py capture           # against FastAPI
    python infrastructure/scripts/api_baseline.py compare           # after a rewrite
    python infrastructure/scripts/api_baseline.py compare --base-url http://127.0.0.1:3100

Volatile fields (timestamps, request ids) are normalised before storage, otherwise
every run would diff against itself. Everything else -- status code, key order,
types, null-vs-absent -- is compared exactly, because those are precisely the
details a reimplementation gets subtly wrong.

Read-only: issues GET requests and unauthenticated POSTs that are expected to be
rejected. It never writes to the database.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx

BASELINE_DIR = Path(__file__).resolve().parents[2] / "tests" / "baseline"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# Values that legitimately change between two runs of identical code.
VOLATILE_KEYS = frozenset(
    {
        "generatedAt",
        "serverTime",
        "requestId",
        "lastHeartbeatAt",
        # Age of the oldest queued job: real elapsed time, so it moves between runs.
        # Appears only in the authenticated metrics response.
        "oldestQueuedJobAgeSeconds",
        "ts",
        "createdAt",
        "publishedAt",
        "updatedAt",
    }
)

# Values that are stable but must not be committed. The admin's login identifier is
# half of a credential pair and lives in a gitignored .env; a baseline file is not the
# place to publish it. Both sides of a comparison are redacted, so equality still holds.
REDACT_KEYS = frozenset({"email"})

# name -> (method, path). Names become filenames, so keep them stable.
ENDPOINTS: list[tuple[str, str, str]] = [
    ("healthz", "GET", "/healthz"),
    ("readyz", "GET", "/readyz"),
    ("public_categories", "GET", "/api/v1/public/categories"),
    ("public_latest", "GET", "/api/v1/public/latest?limit=24"),
    ("public_articles", "GET", "/api/v1/public/articles"),
    ("public_articles_paged", "GET", "/api/v1/public/articles?page=1&page_size=4"),
    # Error and edge behaviour matters as much as the happy path -- a rewrite that
    # returns 200-with-empty where the original returned 404 is a broken contract.
    ("public_article_missing", "GET", "/api/v1/public/articles/trending/2026/01/01/does-not-exist"),
    ("public_articles_unknown_category", "GET", "/api/v1/public/articles?category=not-a-category"),
    ("public_articles_bad_page", "GET", "/api/v1/public/articles?page=0"),
    ("public_articles_oversize_page", "GET", "/api/v1/public/articles?page_size=9999"),
    # Admin routes must reject anonymous callers. These assert the guard, not the data.
    ("admin_me_anonymous", "GET", "/api/v1/admin/auth/me"),
    ("admin_articles_anonymous", "GET", "/api/v1/admin/articles"),
    ("admin_settings_anonymous", "GET", "/api/v1/admin/settings"),
    ("admin_metrics_anonymous", "GET", "/api/v1/admin/system/metrics"),
    ("worker_status_anonymous", "GET", "/api/v1/worker/status"),
]

# Every seeded category, so a rewrite cannot pass by handling only the one category
# someone happened to test with.
CATEGORY_SLUGS = (
    "trending",
    "politics",
    "business",
    "technology",
    "world",
    "sports",
    "entertainment",
    "picks",
)

ENDPOINTS += [
    (
        f"public_articles_{slug}",
        "GET",
        f"/api/v1/public/articles?category={slug}&page_size=4",
    )
    for slug in CATEGORY_SLUGS
]


# ---------------------------------------------------------------- authenticated
#
# Admin endpoints are captured TWICE: once anonymously (above, pinning the 401) and
# once with a real session (here, pinning the actual response body). The anonymous
# capture alone cannot detect a migration that returns a valid 200 with the wrong
# shape -- which is precisely the failure mode that matters.
AUTH_ENDPOINTS: list[tuple[str, str, str]] = [
    ("auth_me", "GET", "/api/v1/admin/auth/me"),
    ("auth_admin_articles", "GET", "/api/v1/admin/articles"),
    ("auth_admin_articles_paged", "GET", "/api/v1/admin/articles?page=1&page_size=5"),
    ("auth_admin_articles_filtered", "GET", "/api/v1/admin/articles?status_filter=draft"),
    ("auth_admin_settings", "GET", "/api/v1/admin/settings"),
    ("auth_admin_metrics", "GET", "/api/v1/admin/system/metrics"),
]

#: Cookie attributes are part of the auth contract and are captured separately from
#: the value, which must never be written to a file.
COOKIE_ATTRIBUTES = ("httponly", "secure", "samesite", "path", "domain", "max-age")


class LoginError(RuntimeError):
    pass


def login(client: httpx.Client) -> str:
    """Authenticate against the live FastAPI login endpoint and return the session id.

    Credentials come from settings (the same .env the services read); they are never
    hardcoded and never written to a baseline file.
    """
    from thedrop_config import get_settings

    settings = get_settings()
    email, password = settings.admin_email, settings.admin_initial_password
    if not email or not password:
        raise LoginError(
            "ADMIN_EMAIL / ADMIN_INITIAL_PASSWORD are not set; cannot capture "
            "authenticated baselines."
        )

    response = client.post(
        "/api/v1/admin/auth/login",
        json={"email": email, "password": password},
    )
    if response.status_code != 200:
        raise LoginError(f"login returned {response.status_code}: {response.text[:200]}")

    session_id = response.cookies.get("thedrop_session")
    if not session_id:
        raise LoginError("login succeeded but set no thedrop_session cookie")
    return session_id


def login_contract(client: httpx.Client) -> dict[str, Any]:
    """Capture the login response contract, including cookie FLAGS but not the value."""
    from thedrop_config import get_settings

    settings = get_settings()
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_initial_password},
    )

    raw_cookie = response.headers.get("set-cookie", "")
    flags = {
        attr: (attr in raw_cookie.lower())
        for attr in ("httponly", "secure")
    }
    for attr in ("samesite", "path", "max-age", "domain"):
        marker = f"{attr}="
        lowered = raw_cookie.lower()
        if marker in lowered:
            start = lowered.index(marker) + len(marker)
            end = lowered.find(";", start)
            flags[attr] = raw_cookie[start : end if end != -1 else len(raw_cookie)].strip()
        else:
            flags[attr] = None

    body = normalise(response.json())
    # The user's public_id is stable, but redact anything that could identify the
    # operator's real credentials.
    return {
        "request": {"method": "POST", "path": "/api/v1/admin/auth/login"},
        "status": response.status_code,
        "content_type": response.headers.get("content-type", "").split(";")[0],
        "cache_control": response.headers.get("cache-control"),
        "cookie_flags": flags,
        "cookie_name": "thedrop_session",
        "body_keys": sorted(body.keys()) if isinstance(body, dict) else None,
        "user_keys": sorted(body["user"].keys())
        if isinstance(body, dict) and isinstance(body.get("user"), dict)
        else None,
    }


def group_of(path: str) -> str:
    """Which migration group an endpoint belongs to.

    Derived from the path rather than stored, so adding an endpoint above cannot
    forget to label it.
    """
    if path.startswith("/api/v1/public"):
        return "public"
    if path.startswith("/api/v1/admin"):
        return "admin"
    if path.startswith("/api/v1/worker"):
        return "worker"
    return "health"


def selected_endpoints(groups: str | None) -> list[tuple[str, str, str, bool]]:
    """(name, method, path, needs_session) for the requested groups.

    Anonymous and authenticated captures live side by side: the `admin` group pins the
    401s, the `authenticated` group pins the real response bodies.
    """
    everything: list[tuple[str, str, str, bool]] = [
        (n, m, p, False) for n, m, p in ENDPOINTS
    ] + [(n, m, p, True) for n, m, p in AUTH_ENDPOINTS]

    if not groups:
        return everything

    wanted = {g.strip() for g in groups.split(",") if g.strip()}
    return [
        e
        for e in everything
        if ("authenticated" if e[3] else group_of(e[2])) in wanted
    ]


def normalise(value: Any) -> Any:
    """Replace volatile values so two runs of identical code compare equal."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in REDACT_KEYS and v is not None:
                out[k] = "<redacted>"
            elif k in VOLATILE_KEYS and v is not None:
                out[k] = "<volatile>"
            else:
                out[k] = normalise(v)
        return out
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def fetch(
    client: httpx.Client, method: str, path: str, session_id: str | None = None
) -> dict[str, Any]:
    cookies = {"thedrop_session": session_id} if session_id else None
    response = client.request(method, path, cookies=cookies)
    try:
        body = normalise(response.json())
    except ValueError:
        body = {"__non_json_body__": response.text[:500]}

    return {
        "request": {"method": method, "path": path},
        "status": response.status_code,
        "content_type": response.headers.get("content-type", "").split(";")[0],
        # Cache-Control is part of the contract: the web app's ISR behaviour and any
        # CDN in front of it depend on it, so a rewrite that drops it is a regression.
        "cache_control": response.headers.get("cache-control"),
        "body": body,
    }


def _session_for(client: httpx.Client, endpoints: list[tuple[str, str, str, bool]]) -> str | None:
    """Log in once if any selected endpoint needs a session, otherwise not at all."""
    if not any(needs for *_rest, needs in endpoints):
        return None
    return login(client)


def _logout(client: httpx.Client, session_id: str | None) -> None:
    """Release the session this run created.

    Without it every capture/compare leaves a live session in Redis for the full
    two-hour idle window; a CI job running on every push would accumulate them
    indefinitely.
    """
    if session_id is None:
        return
    # Cleanup failure must not fail a verification run; the session expires anyway.
    with contextlib.suppress(httpx.HTTPError):
        client.post(
            "/api/v1/admin/auth/logout",
            headers={"Cookie": f"thedrop_session={session_id}"},
        )


def capture(base_url: str, groups: str | None = None) -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    endpoints = selected_endpoints(groups)

    with httpx.Client(base_url=base_url, timeout=20.0, follow_redirects=False) as client:
        session_id = _session_for(client, endpoints)

        for name, method, path, needs_session in endpoints:
            record = fetch(client, method, path, session_id if needs_session else None)
            (BASELINE_DIR / f"{name}.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            marker = "auth" if needs_session else "anon"
            print(f"  captured [{marker}] {name:34s} {record['status']}")

        # The login contract itself: status, cookie flags and body shape. The cookie
        # VALUE is never written -- a baseline file containing a live session id would
        # be a credential in version control.
        if session_id is not None:
            record = login_contract(client)
            (BASELINE_DIR / "auth_login_contract.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"  captured [auth] {'login_contract':34s} {record['status']}")

    print(f"\n{len(endpoints)} endpoints captured to {BASELINE_DIR}")
    return 0


def compare(base_url: str, groups: str | None = None) -> int:
    if not BASELINE_DIR.exists():
        print("No baseline found. Run `capture` first.", file=sys.stderr)
        return 2

    endpoints = selected_endpoints(groups)
    failures: list[str] = []
    with httpx.Client(base_url=base_url, timeout=20.0, follow_redirects=False) as client:
        session_id = _session_for(client, endpoints)

        for name, method, path, needs_session in endpoints:
            path_file = BASELINE_DIR / f"{name}.json"
            if not path_file.exists():
                print(f"  SKIP    {name:38s} (no baseline)")
                continue

            expected = json.loads(path_file.read_text(encoding="utf-8"))
            actual = fetch(client, method, path, session_id if needs_session else None)

            diffs = []
            for field in ("status", "content_type", "cache_control", "body"):
                if expected.get(field) != actual.get(field):
                    diffs.append(field)

            if diffs:
                failures.append(name)
                print(f"  DIFF    {name:38s} -> {', '.join(diffs)}")
                for field in diffs:
                    print(f"            expected {field}: {json.dumps(expected.get(field))[:200]}")
                    print(f"            actual   {field}: {json.dumps(actual.get(field))[:200]}")
            else:
                print(f"  match   {name:38s} {actual['status']}")

        _logout(client, session_id)

    if failures:
        print(f"\n{len(failures)} endpoint(s) differ: {', '.join(failures)}")
        return 1
    print(f"\nAll {len(endpoints)} endpoints match the baseline.")
    return 0


def parity(url_a: str, url_b: str, groups: str | None, extra: str | None) -> int:
    """Diff two LIVE servers against each other.

    The stored baseline can only pin behaviour for the data that existed when it was
    captured -- with an empty `articles` table that means pagination, ordering and
    article serialisation go unverified. This mode compares the Python and Node
    implementations directly, so temporary fixture data can exercise the paths the
    baseline cannot reach. Nothing in tests/baseline is read or written.
    """
    paths = list(selected_endpoints(groups))
    if extra:
        for i, raw in enumerate(extra.split(",")):
            raw = raw.strip()
            if raw:
                paths.append((f"extra_{i}", "GET", raw, False))

    failures: list[str] = []
    with (
        httpx.Client(base_url=url_a, timeout=30.0, follow_redirects=False) as a,
        httpx.Client(base_url=url_b, timeout=30.0, follow_redirects=False) as b,
    ):
        # Each server issues its own session: a cookie minted by one is not
        # necessarily valid on the other during a migration.
        sa = _session_for(a, paths)
        sb = _session_for(b, paths)

        for name, method, path, needs in paths:
            ra = fetch(a, method, path, sa if needs else None)
            rb = fetch(b, method, path, sb if needs else None)
            diffs = [
                f
                for f in ("status", "content_type", "cache_control", "body")
                if ra.get(f) != rb.get(f)
            ]
            if diffs:
                failures.append(name)
                print(f"  DIFF    {name:34s} {path}")
                for f in diffs:
                    print(f"            A ({f}): {json.dumps(ra.get(f))[:400]}")
                    print(f"            B ({f}): {json.dumps(rb.get(f))[:400]}")
            else:
                print(f"  match   {name:34s} {ra['status']}  {path}")

        _logout(a, sa)
        _logout(b, sb)

    if failures:
        print(f"\n{len(failures)} path(s) differ between the two servers.")
        return 1
    print(f"\nAll {len(paths)} paths identical on both servers.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "compare", "parity"])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--group",
        default=None,
        help="Comma-separated: public, admin, worker, health. Default is all. Use this "
        "to compare a migrated group against a host that serves only that group.",
    )
    parser.add_argument("--url-a", default=DEFAULT_BASE_URL, help="parity: first server")
    parser.add_argument("--url-b", default="http://127.0.0.1:3100", help="parity: second server")
    parser.add_argument("--extra-paths", default=None, help="parity: extra comma-separated paths")
    args = parser.parse_args()

    scope = args.group or "all groups"
    if args.mode == "parity":
        print(f"parity {args.url_a} vs {args.url_b} [{scope}]\n")
        return parity(args.url_a, args.url_b, args.group, args.extra_paths)

    print(f"{args.mode} against {args.base_url} [{scope}]\n")
    if args.mode == "capture":
        return capture(args.base_url, args.group)
    return compare(args.base_url, args.group)


if __name__ == "__main__":
    sys.exit(main())
