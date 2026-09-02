"""The database-touching half of ingestion.

Split by what actually needs a database:

  * The adapter registry, source auto-creation policy and circuit-breaker arithmetic
    are decisions, not queries. They are tested directly, with no session, because
    they are the parts most likely to be quietly changed and least likely to be
    noticed.
  * The dedup cascade and `poll` need real SQL -- band arithmetic evaluated by
    Postgres, a unique-constraint race, JSONB round-tripping. **They are not covered
    here.** No local Postgres was reachable when this was written, and a test authored
    against a database it never ran on is worse than none: it looks like coverage.

    Specifically unverified: the bit-shift band predicates in `_nearest_by_simhash`
    (Postgres `>>` and `&` on a signed bigint), the IntegrityError path in
    `store_item`, and `poll` end to end. That is a real gap, not an oversight.

The registry is the security-relevant one. `providers.adapter_class` is documented as
a dotted path "resolved at runtime"; importing whatever string sits in that column
would turn any write access to the `providers` table into arbitrary code execution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from thedrop_database.enums import CircuitState, SourceType
from thedrop_ingest.pipeline import (
    ADAPTER_REGISTRY,
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_OPEN_DURATION,
    AdapterNotRegisteredError,
    _circuit_allows,
    _record_failure,
    _record_success,
    build_adapter,
)
from thedrop_ingest.providers import ProviderError, ProviderPage
from thedrop_ingest.providers.rss import RSSProvider

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class FakeProvider:
    """Stands in for the ORM object; `poll` only ever touches these attributes."""

    def __init__(self, **kwargs: object) -> None:
        self.slug = "example"
        self.adapter_class = "thedrop_ingest.providers.rss.RSSProvider"
        self.config: dict[str, object] = {"feed_url": "https://example.com/feed.xml"}
        self.enabled = True
        self.circuit_state = CircuitState.CLOSED
        self.circuit_opened_at: datetime | None = None
        self.consecutive_failures = 0
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.cursor: str | None = None
        for key, value in kwargs.items():
            setattr(self, key, value)


# ------------------------------------------------------------------ adapter registry


def test_known_adapter_builds() -> None:
    adapter = build_adapter(FakeProvider())
    assert isinstance(adapter, RSSProvider)
    assert adapter.feed_url == "https://example.com/feed.xml"


def test_unregistered_adapter_is_refused_not_imported() -> None:
    """The registry is the point: a dotted path from the database is never imported.

    `adapter_class` is operator-supplied data. Resolving it with importlib would make
    write access to one table equivalent to code execution, so an unknown value has to
    be a loud error rather than an import attempt.
    """
    provider = FakeProvider(adapter_class="os.system")

    with pytest.raises(AdapterNotRegisteredError, match="not registered"):
        build_adapter(provider)


def test_registry_rejects_a_plausible_but_absent_path() -> None:
    provider = FakeProvider(adapter_class="thedrop_ingest.providers.rss.NotAProvider")

    with pytest.raises(AdapterNotRegisteredError):
        build_adapter(provider)


def test_every_registered_value_is_the_class_it_names() -> None:
    """A registry whose keys drift from its values is worse than no registry."""
    for dotted, cls in ADAPTER_REGISTRY.items():
        assert dotted == f"{cls.__module__}.{cls.__qualname__}"


def test_missing_feed_url_is_a_configuration_error() -> None:
    with pytest.raises(ProviderError, match="feed_url"):
        build_adapter(FakeProvider(config={}))


# ------------------------------------------------------------------ circuit breaker


def test_closed_circuit_allows_polling() -> None:
    assert _circuit_allows(FakeProvider(), NOW) is True


def test_failures_below_the_threshold_do_not_open_the_circuit() -> None:
    provider = FakeProvider()
    for _ in range(CIRCUIT_FAILURE_THRESHOLD - 1):
        _record_failure(provider, "timeout", NOW)

    assert provider.circuit_state == CircuitState.CLOSED
    assert _circuit_allows(provider, NOW) is True


def test_threshold_failures_open_the_circuit() -> None:
    provider = FakeProvider()
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        _record_failure(provider, "timeout", NOW)

    assert provider.circuit_state == CircuitState.OPEN
    assert _circuit_allows(provider, NOW) is False


def test_open_circuit_blocks_until_the_window_elapses() -> None:
    provider = FakeProvider(circuit_state=CircuitState.OPEN, circuit_opened_at=NOW)

    assert _circuit_allows(provider, NOW + CIRCUIT_OPEN_DURATION - timedelta(seconds=1)) is False


def test_circuit_half_opens_for_one_probe_after_the_window() -> None:
    provider = FakeProvider(circuit_state=CircuitState.OPEN, circuit_opened_at=NOW)

    assert _circuit_allows(provider, NOW + CIRCUIT_OPEN_DURATION) is True
    assert provider.circuit_state == CircuitState.HALF_OPEN


def test_success_closes_the_circuit_and_clears_the_error() -> None:
    provider = FakeProvider(
        circuit_state=CircuitState.HALF_OPEN,
        circuit_opened_at=NOW,
        consecutive_failures=7,
        last_error="timeout",
    )

    _record_success(provider, ProviderPage(items=(), next_cursor="cursor-1"), NOW)

    assert provider.circuit_state == CircuitState.CLOSED
    assert provider.consecutive_failures == 0
    assert provider.circuit_opened_at is None
    assert provider.last_error is None
    assert provider.cursor == "cursor-1"


def test_success_without_a_cursor_keeps_the_previous_one() -> None:
    """A feed that sends no Last-Modified must not lose the cursor we already had."""
    provider = FakeProvider(cursor="cursor-1")

    _record_success(provider, ProviderPage(items=(), next_cursor=None), NOW)

    assert provider.cursor == "cursor-1"


def test_a_failure_after_a_half_open_probe_reopens_the_circuit() -> None:
    provider = FakeProvider(
        circuit_state=CircuitState.HALF_OPEN,
        consecutive_failures=CIRCUIT_FAILURE_THRESHOLD - 1,
    )

    _record_failure(provider, "still down", NOW)

    assert provider.circuit_state == CircuitState.OPEN


def test_error_text_is_truncated_before_storage() -> None:
    provider = FakeProvider()
    _record_failure(provider, "x" * 5000, NOW)

    assert len(provider.last_error) <= 2000


# ------------------------------------------------------------------ source policy


def test_authority_suffixes_are_recognised_by_tld_not_by_judgement() -> None:
    """`.gov` is a fact about the domain. Reliability is not, and is never guessed.

    A new source starts untrusted regardless: it can contribute context, but cannot
    satisfy a corroboration requirement alone until it is classified.
    """
    from thedrop_ingest.pipeline import _AUTHORITY_SUFFIXES

    assert "senate.gov".endswith(_AUTHORITY_SUFFIXES)
    assert "army.mil".endswith(_AUTHORITY_SUFFIXES)
    assert not "example.com".endswith(_AUTHORITY_SUFFIXES)
    # A lookalike must not qualify: the suffix has to be the actual TLD.
    assert not "notreally-gov.com".endswith(_AUTHORITY_SUFFIXES)


def test_source_type_enum_has_the_values_auto_creation_uses() -> None:
    assert SourceType.GOVERNMENT
    assert SourceType.UNKNOWN
