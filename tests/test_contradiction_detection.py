"""Contradiction detection's Ollama path (agent.contradictions), PIPELINE.md §11.

Same shape as tests/test_claim_extraction.py: retry-once semantics, the
untrusted-data wrapping, and the index-translation this module owns so callers never
have to independently reconstruct which claims were checkable.

Everything runs against an httpx.MockTransport, matching the established pattern --
no Ollama server needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "agent-runner"))

from agent import contradictions  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("CONTRADICTION_CHECK_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("CLAIM_EXTRACT_OLLAMA_MODEL", raising=False)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _claim(text: str, claim_type: str = "FACT", attributed_to: str | None = None) -> dict[str, str]:
    return {"claim_text": text, "claim_type": claim_type, "attributed_to": attributed_to or ""}


# --------------------------------------------------------------------- is_available


def test_is_available_falls_back_to_the_extraction_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIM_EXTRACT_OLLAMA_MODEL", "qwen2.5:7b")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})

    _patch_transport(monkeypatch, handler)
    assert contradictions.is_available() is True


def test_is_available_prefers_its_own_model_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIM_EXTRACT_OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("CONTRADICTION_CHECK_OLLAMA_MODEL", "qwen2.5:14b")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:14b"}]})

    _patch_transport(monkeypatch, handler)
    assert contradictions.is_available() is True


# ------------------------------------------------------------------ short-circuit


def test_fewer_than_two_checkable_claims_skips_the_model_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never call Ollama for < 2 checkable claims")

    _patch_transport(monkeypatch, handler)
    result = contradictions.find_contradictions([_claim("only one fact")])
    assert result.contradictions == []


def test_opinions_do_not_count_toward_the_checkable_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("two OPINIONs must not trigger a model call")

    _patch_transport(monkeypatch, handler)
    claims = [_claim("x is good", "OPINION"), _claim("x is bad", "OPINION")]
    result = contradictions.find_contradictions(claims)
    assert result.contradictions == []


# -------------------------------------------------------------------- index mapping


def test_indices_are_translated_back_to_the_callers_original_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model only ever sees the CHECKABLE subset (claim 0, an OPINION, is
    excluded), so its local index 0/1 refers to the caller's indices 1 and 2. The
    caller must get back indices valid against ITS OWN original list."""
    response = {
        "contradictions": [{"claim_index_a": 0, "claim_index_b": 1, "reason": "conflict"}],
        "injection_detected": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # The model was only shown the two FACT claims, not the OPINION.
        assert "opinion text" not in body["messages"][1]["content"]
        return httpx.Response(200, json={"message": {"content": json.dumps(response)}})

    _patch_transport(monkeypatch, handler)
    claims = [
        _claim("opinion text", "OPINION"),
        _claim("the jury reached a verdict"),
        _claim("the jury remains deadlocked"),
    ]
    result = contradictions.find_contradictions(claims)

    assert len(result.contradictions) == 1
    pair = result.contradictions[0]
    assert {pair.claim_index_a, pair.claim_index_b} == {1, 2}


def test_an_out_of_range_index_is_dropped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "contradictions": [{"claim_index_a": 0, "claim_index_b": 99, "reason": "bogus"}],
        "injection_detected": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": json.dumps(response)}})

    _patch_transport(monkeypatch, handler)
    claims = [_claim("a fact"), _claim("another fact")]
    result = contradictions.find_contradictions(claims)

    assert result.contradictions == []


# -------------------------------------------------------------------------- retry


def test_retries_once_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    valid = {
        "contradictions": [{"claim_index_a": 0, "claim_index_b": 1, "reason": "x"}],
        "injection_detected": False,
    }
    responses = iter(
        [{"message": {"content": "not json"}}, {"message": {"content": json.dumps(valid)}}]
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=next(responses))

    _patch_transport(monkeypatch, handler)
    claims = [_claim("fact one"), _claim("fact two")]
    result = contradictions.find_contradictions(claims)

    assert len(calls) == 2
    assert len(result.contradictions) == 1


def test_fails_after_two_invalid_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "still not json"}})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(contradictions.ContradictionCheckFailedError):
        contradictions.find_contradictions([_claim("fact one"), _claim("fact two")])


# ------------------------------------------------------------------- ContradictionPair


def test_a_claim_cannot_contradict_itself() -> None:
    with pytest.raises(Exception, match="cannot contradict itself"):
        contradictions.ContradictionPair(claim_index_a=1, claim_index_b=1, reason="x")
