"""Python vs Node parity for the migrated admin read endpoints.

Covers GET /admin/articles, /admin/settings and /admin/system/metrics across every
principal, every documented edge case, and — for the first time in this migration —
DATE SERIALISATION with real rows.

Why dates need their own coverage: `publishedAt`, `createdAt` and `updatedAt` are all
in the baseline's VOLATILE_KEYS, so every comparison so far normalised them away. This
file compares them raw. Fixture articles deliberately include a timestamp with zero
microseconds and one with non-zero microseconds, because Python's `isoformat()` omits
the fractional part in the first case and JavaScript's `toISOString()` never does.

Requires Postgres, Redis, FastAPI and Next.js.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis as redis_lib
from rbac_fixtures import (
    FixtureSet,
    cleanup_fixture_sessions,
    cleanup_fixture_users,
    create_fixture_users,
)
from sqlalchemy import delete, select
from thedrop_config import get_settings
from thedrop_database import session_scope
from thedrop_database.enums import ArticleProvenance
from thedrop_database.models import Article, Category

pytestmark = [
    pytest.mark.db,
    pytest.mark.redis,
    pytest.mark.api,
    pytest.mark.web,
    pytest.mark.integration,
]

SESSION_COOKIE = "thedrop_session"
ARTICLE_PREFIX = "zzadmin-"

#: Compared on every request. `vary` is excluded — Next.js appends its RSC routing
#: header to every App Router response and it cannot be suppressed without fighting
#: the framework. It is inert here because these responses carry no Cache-Control.
#: This is the ONLY tolerated header difference and it is asserted to be the only one.
COMPARED_HEADERS = ("content-type", "cache-control", "x-frame-options", "www-authenticate")

IGNORED_HEADERS = {
    "vary", "date", "server", "connection", "keep-alive",
    "content-length", "transfer-encoding", "x-request-id", "content-encoding",
}

ENDPOINTS = [
    "/api/v1/admin/articles",
    "/api/v1/admin/settings",
    "/api/v1/admin/system/metrics",
]

PRINCIPALS = ("anonymous", "admin", "editor", "analyst", "viewer", "multi")

#: Volatile between two calls even against identical data.
VOLATILE = {"generatedAt", "oldestQueuedJobAgeSeconds", "lastHeartbeatAt", "requestId"}


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


@pytest.fixture(scope="module")
def articles() -> Iterator[int]:
    """Nine articles across statuses, with deliberately varied timestamps.

    Includes a zero-microsecond and a non-zero-microsecond value so date
    serialisation is actually exercised, and a soft-deleted row that must stay
    invisible to both implementations.
    """
    with session_scope() as db:
        category = db.scalar(select(Category).where(Category.slug == "trending"))
        assert category is not None

        base = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        statuses = ["draft", "draft", "qa", "approved", "published", "published", "rejected"]

        for i, status in enumerate(statuses):
            # Alternate microsecond precision: 0 for even indices, non-zero for odd.
            micro = 0 if i % 2 == 0 else 100000 + i
            stamp = base + timedelta(hours=i, microseconds=micro)
            db.add(
                Article(
                    slug=f"{ARTICLE_PREFIX}{i}",
                    category_id=category.id,
                    article_type="NEWS" if i % 2 == 0 else "ANALYSIS",
                    headline=f"Admin parity article {i}",
                    dek=f"Dek {i}",
                    # Fixtures state provenance explicitly because every writer must:
                    # the column has no default precisely so an omission fails loudly
                    # rather than defaulting into the value that escapes the
                    # traceability constraint.
                    provenance=ArticleProvenance.MANUAL,
                    status=status,
                    risk_tier="high" if i == 3 else "standard",
                    editorial_confidence=None if i == 0 else 70 + i,
                    published_at=stamp if status == "published" else None,
                    first_published_at=stamp if status == "published" else None,
                    word_count=100 + i,
                    reading_time_seconds=60 + i,
                )
            )

        # Soft-deleted: must be excluded by both implementations.
        db.add(
            Article(
                slug=f"{ARTICLE_PREFIX}deleted",
                category_id=category.id,
                article_type="NEWS",
                headline="Soft deleted",
                dek="",
                provenance=ArticleProvenance.MANUAL,
                status="published",
                deleted_at=datetime.now(UTC),
            )
        )

    yield len(statuses)

    with session_scope() as db:
        db.execute(delete(Article).where(Article.slug.like(f"{ARTICLE_PREFIX}%")))


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


def _session(client: httpx.Client, fixtures: FixtureSet, principal: str) -> str | None:
    if principal == "anonymous":
        return None
    user = fixtures[principal]
    return _login(client, user.email, user.password)


def _get(client: httpx.Client, path: str, sid: str | None) -> httpx.Response:
    client.cookies.clear()
    headers = {"Cookie": f"{SESSION_COOKIE}={sid}"} if sid else {}
    return client.get(path, headers=headers)


def _strip_volatile(value):
    if isinstance(value, dict):
        return {
            k: ("<volatile>" if k in VOLATILE and v is not None else _strip_volatile(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def _assert_identical(py: httpx.Response, node: httpx.Response, label: str) -> None:
    assert py.status_code == node.status_code, (
        f"{label}: status python={py.status_code} node={node.status_code}"
    )

    py_body = _strip_volatile(py.json())
    node_body = _strip_volatile(node.json())
    assert py_body == node_body, f"{label}: body\n  python={py_body}\n  node={node_body}"

    for header in COMPARED_HEADERS:
        assert py.headers.get(header) == node.headers.get(header), (
            f"{label}: header {header}: "
            f"python={py.headers.get(header)!r} node={node.headers.get(header)!r}"
        )


class TestRbacParity:
    """Every endpoint, every principal, both servers."""

    @pytest.mark.parametrize("path", ENDPOINTS)
    @pytest.mark.parametrize("principal", PRINCIPALS)
    def test_identical(
        self, py_client, node_client, fixtures, articles, principal: str, path: str
    ) -> None:
        sid = _session(py_client, fixtures, principal)
        _assert_identical(
            _get(py_client, path, sid),
            _get(node_client, path, sid),
            f"{principal} {path}",
        )

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_no_unexpected_header_divergence(
        self, py_client, node_client, fixtures, articles, path: str
    ) -> None:
        """Pins the framework-header exception: nothing beyond `vary` may differ."""
        sid = _session(py_client, fixtures, "admin")
        py, node = _get(py_client, path, sid), _get(node_client, path, sid)

        py_h = {k.lower(): v for k, v in py.headers.items() if k.lower() not in IGNORED_HEADERS}
        node_h = {
            k.lower(): v for k, v in node.headers.items() if k.lower() not in IGNORED_HEADERS
        }
        assert py_h == node_h, f"{path}: python={py_h} node={node_h}"


class TestArticleListParity:
    @pytest.mark.parametrize(
        "qs",
        [
            "",
            "?page=1&page_size=5",
            "?page=2&page_size=3",
            "?page=3&page_size=3",
            "?page=99&page_size=10",
            "?page_size=0",
            "?page_size=100000",
            "?status_filter=draft",
            "?status_filter=published",
            "?status_filter=not-a-status",
            "?status_filter=",
            "?page=1&page_size=2&status_filter=draft",
        ],
    )
    def test_pagination_and_filtering(
        self, py_client, node_client, fixtures, articles, qs: str
    ) -> None:
        sid = _session(py_client, fixtures, "admin")
        path = f"/api/v1/admin/articles{qs}"
        _assert_identical(_get(py_client, path, sid), _get(node_client, path, sid), path)

    @pytest.mark.parametrize("qs", ["?page=0", "?page=-3", "?page_size=-1"])
    def test_out_of_range_produces_the_same_500(
        self, py_client, node_client, fixtures, articles, qs: str
    ) -> None:
        """Pre-existing FastAPI defect, reproduced rather than fixed.

        `/admin/articles` declares page/page_size without ge/le bounds, so a negative
        OFFSET or LIMIT reaches PostgreSQL and raises. Both tiers therefore return a
        500 with the same body. Correcting this belongs to a separate change, not to a
        migration whose whole purpose is behavioural equivalence.
        """
        sid = _session(py_client, fixtures, "admin")
        path = f"/api/v1/admin/articles{qs}"
        py, node = _get(py_client, path, sid), _get(node_client, path, sid)

        assert py.status_code == 500, f"expected the known defect, got {py.status_code}"
        assert node.status_code == 500
        assert py.json()["detail"] == node.json()["detail"] == "Internal server error"

    @pytest.mark.parametrize("qs", ["?page=abc", "?page_size=abc", "?page=1.5"])
    def test_non_integer_is_422_in_both(
        self, py_client, node_client, fixtures, articles, qs: str
    ) -> None:
        sid = _session(py_client, fixtures, "admin")
        path = f"/api/v1/admin/articles{qs}"
        _assert_identical(_get(py_client, path, sid), _get(node_client, path, sid), path)

    def test_true_count_not_an_estimate(self, py_client, node_client, fixtures, articles) -> None:
        """Admin uses COUNT(*); the public endpoint uses an estimate. Do not unify."""
        sid = _session(py_client, fixtures, "admin")
        path = "/api/v1/admin/articles?page=1&page_size=2"

        py = _get(py_client, path, sid).json()
        node = _get(node_client, path, sid).json()

        assert py["total"] == node["total"] == articles, (
            "total must be the real row count, not offset+len+1"
        )
        assert len(py["items"]) == len(node["items"]) == 2

    def test_soft_deleted_rows_are_invisible_to_both(
        self, py_client, node_client, fixtures, articles
    ) -> None:
        sid = _session(py_client, fixtures, "admin")
        for client in (py_client, node_client):
            body = _get(client, "/api/v1/admin/articles?page_size=100", sid).json()
            slugs = [item["slug"] for item in body["items"]]
            assert f"{ARTICLE_PREFIX}deleted" not in slugs

    def test_ordering_is_created_at_desc_in_both(
        self, py_client, node_client, fixtures, articles
    ) -> None:
        sid = _session(py_client, fixtures, "admin")
        path = "/api/v1/admin/articles?page_size=100"
        py = [i["id"] for i in _get(py_client, path, sid).json()["items"]]
        node = [i["id"] for i in _get(node_client, path, sid).json()["items"]]
        assert py == node


class TestDateSerialisation:
    """The gap the baseline could never close: timestamps are normalised there."""

    def test_dates_are_byte_identical(self, py_client, node_client, fixtures, articles) -> None:
        sid = _session(py_client, fixtures, "admin")
        path = "/api/v1/admin/articles?page_size=100"

        py_items = _get(py_client, path, sid).json()["items"]
        node_items = _get(node_client, path, sid).json()["items"]

        py_dates = {i["slug"]: (i["publishedAt"], i["createdAt"]) for i in py_items}
        node_dates = {i["slug"]: (i["publishedAt"], i["createdAt"]) for i in node_items}
        assert py_dates == node_dates

    def test_zero_microseconds_omits_the_fraction(
        self, py_client, node_client, fixtures, articles
    ) -> None:
        """Python drops `.000000`; a naive JS port emits it. Both must drop it."""
        sid = _session(py_client, fixtures, "admin")
        path = "/api/v1/admin/articles?page_size=100"

        for client in (py_client, node_client):
            published = [
                i["publishedAt"]
                for i in _get(client, path, sid).json()["items"]
                if i["publishedAt"] is not None
            ]
            assert published, "fixture produced no published articles"
            assert any("." not in p for p in published), (
                f"expected at least one timestamp without a fraction, got {published}"
            )

    def test_microsecond_precision_survives(
        self, py_client, node_client, fixtures, articles
    ) -> None:
        """A JavaScript Date would truncate microseconds to milliseconds."""
        sid = _session(py_client, fixtures, "admin")
        path = "/api/v1/admin/articles?page_size=100"

        for name, client in (("python", py_client), ("node", node_client)):
            fractions = [
                p.split(".")[1].split("+")[0]
                for p in (
                    i["publishedAt"]
                    for i in _get(client, path, sid).json()["items"]
                    if i["publishedAt"]
                )
                if "." in p
            ]
            assert fractions, f"{name}: no fractional timestamps in the fixture set"
            assert all(len(f) == 6 for f in fractions), f"{name}: {fractions}"
            assert any(not f.endswith("000") for f in fractions), (
                f"{name}: sub-millisecond precision was lost -> {fractions}"
            )


class TestSettingsParity:
    def test_ordering_and_fields(self, py_client, node_client, fixtures) -> None:
        sid = _session(py_client, fixtures, "admin")
        py = _get(py_client, "/api/v1/admin/settings", sid).json()
        node = _get(node_client, "/api/v1/admin/settings", sid).json()

        assert py == node
        assert [s["key"] for s in node] == sorted(s["key"] for s in node)
        assert set(node[0]) == {"key", "value", "description", "isProtected"}

    def test_is_protected_survives(self, py_client, node_client, fixtures) -> None:
        sid = _session(py_client, fixtures, "admin")
        node = _get(node_client, "/api/v1/admin/settings", sid).json()
        protected = {s["key"] for s in node if s["isProtected"]}
        assert "publishing.enabled" in protected
        assert "ai.enabled" in protected


class TestMetricsParity:
    def test_structure_and_counts(self, py_client, node_client, fixtures, articles) -> None:
        sid = _session(py_client, fixtures, "admin")
        py = _get(py_client, "/api/v1/admin/system/metrics", sid).json()
        node = _get(node_client, "/api/v1/admin/system/metrics", sid).json()

        assert set(py) == set(node)
        assert py["articles"] == node["articles"]
        assert py["jobs"] == node["jobs"]
        assert py["workers"] == node["workers"]
        assert py["redis"] == node["redis"] is True

    def test_article_counts_reflect_the_fixtures(
        self, node_client, py_client, fixtures, articles
    ) -> None:
        sid = _session(py_client, fixtures, "admin")
        node = _get(node_client, "/api/v1/admin/system/metrics", sid).json()
        by_status = node["articles"]["byStatus"]
        assert by_status.get("draft") == 2
        assert by_status.get("published") == 2, "soft-deleted rows must be excluded"


class TestSideEffects:
    def test_admin_reads_do_not_change_row_counts(
        self, py_client, node_client, fixtures, articles
    ) -> None:
        from sqlalchemy import func

        def counts() -> tuple[int, int]:
            with session_scope() as db:
                return (
                    db.scalar(select(func.count()).select_from(Article)) or 0,
                    db.scalar(select(func.count()).select_from(Category)) or 0,
                )

        before = counts()
        sid = _session(py_client, fixtures, "admin")
        for path in ENDPOINTS:
            _get(py_client, path, sid)
            _get(node_client, path, sid)
        assert counts() == before

    def test_both_slide_the_session_ttl(self, py_client, node_client, fixtures, r) -> None:
        """Admin reads authenticate, so they must refresh the idle window too."""
        for client in (py_client, node_client):
            sid = _session(py_client, fixtures, "admin")
            key = f"session:{sid}"
            r.expire(key, 60)
            assert _get(client, "/api/v1/admin/settings", sid).status_code == 200
            assert r.ttl(key) > 60
