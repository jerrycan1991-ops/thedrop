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
        "ts",
        "createdAt",
        "publishedAt",
        "updatedAt",
    }
)

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


def normalise(value: Any) -> Any:
    """Replace volatile values so two runs of identical code compare equal."""
    if isinstance(value, dict):
        return {
            k: ("<volatile>" if k in VOLATILE_KEYS and value[k] is not None else normalise(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def fetch(client: httpx.Client, method: str, path: str) -> dict[str, Any]:
    response = client.request(method, path)
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


def capture(base_url: str) -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base_url, timeout=20.0, follow_redirects=False) as client:
        for name, method, path in ENDPOINTS:
            record = fetch(client, method, path)
            (BASELINE_DIR / f"{name}.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"  captured {name:38s} {record['status']}")
    print(f"\n{len(ENDPOINTS)} endpoints captured to {BASELINE_DIR}")
    return 0


def compare(base_url: str) -> int:
    if not BASELINE_DIR.exists():
        print("No baseline found. Run `capture` first.", file=sys.stderr)
        return 2

    failures: list[str] = []
    with httpx.Client(base_url=base_url, timeout=20.0, follow_redirects=False) as client:
        for name, method, path in ENDPOINTS:
            path_file = BASELINE_DIR / f"{name}.json"
            if not path_file.exists():
                print(f"  SKIP    {name:38s} (no baseline)")
                continue

            expected = json.loads(path_file.read_text(encoding="utf-8"))
            actual = fetch(client, method, path)

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

    if failures:
        print(f"\n{len(failures)} endpoint(s) differ: {', '.join(failures)}")
        return 1
    print(f"\nAll {len(ENDPOINTS)} endpoints match the baseline.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "compare"])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    print(f"{args.mode} against {args.base_url}\n")
    return capture(args.base_url) if args.mode == "capture" else compare(args.base_url)


if __name__ == "__main__":
    sys.exit(main())
