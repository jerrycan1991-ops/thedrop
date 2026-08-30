"""Security primitives.

Each test here maps to a specific attack: offline cracking, token theft from a database
dump, and replay of a captured worker request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.security import (
    constant_time_equals,
    hash_password,
    new_worker_token,
    sign_request,
    timestamp_is_fresh,
    token_digest,
    verify_password,
)


class TestPasswordHashing:
    def test_roundtrip(self) -> None:
        digest = hash_password("correct horse battery staple")
        assert verify_password(digest, "correct horse battery staple") is True

    def test_wrong_password_rejected(self) -> None:
        digest = hash_password("correct horse battery staple")
        assert verify_password(digest, "Correct horse battery staple") is False

    def test_hash_is_salted(self) -> None:
        # Identical passwords must not produce identical hashes, or a leaked table
        # reveals which accounts share a password.
        assert hash_password("same") != hash_password("same")

    def test_uses_argon2id(self) -> None:
        assert hash_password("x").startswith("$argon2id$")

    def test_malformed_hash_does_not_raise(self) -> None:
        # A corrupt row must fail the login, not 500 the endpoint.
        assert verify_password("not-a-hash", "anything") is False


class TestWorkerTokens:
    def test_tokens_are_high_entropy(self) -> None:
        token = new_worker_token()
        assert len(token) >= 60

    def test_tokens_are_unique(self) -> None:
        assert len({new_worker_token() for _ in range(50)}) == 50

    def test_digest_is_one_way_and_stable(self) -> None:
        token = new_worker_token()
        assert token_digest(token) == token_digest(token)
        assert token.encode() not in token_digest(token)

    def test_constant_time_comparison(self) -> None:
        a = token_digest("alpha")
        assert constant_time_equals(a, token_digest("alpha")) is True
        assert constant_time_equals(a, token_digest("beta")) is False


class TestRequestSigning:
    def test_signature_is_deterministic(self) -> None:
        args = (
            "secret",
            "POST",
            "/api/v1/worker/heartbeat",
            b'{"a":1}',
            "2026-08-30T00:00:00+00:00",
            "n1",
        )
        assert sign_request(*args) == sign_request(*args)

    def test_signature_covers_the_body(self) -> None:
        # Without body coverage, an attacker could swap the payload of a captured
        # request while keeping a valid signature.
        base = ("secret", "POST", "/p", b'{"a":1}', "2026-08-30T00:00:00+00:00", "n1")
        tampered = ("secret", "POST", "/p", b'{"a":2}', "2026-08-30T00:00:00+00:00", "n1")
        assert sign_request(*base) != sign_request(*tampered)

    def test_signature_covers_the_nonce(self) -> None:
        base = ("secret", "POST", "/p", b"{}", "2026-08-30T00:00:00+00:00", "n1")
        replayed = ("secret", "POST", "/p", b"{}", "2026-08-30T00:00:00+00:00", "n2")
        assert sign_request(*base) != sign_request(*replayed)

    def test_signature_covers_the_path(self) -> None:
        a = ("secret", "POST", "/api/v1/worker/heartbeat", b"{}", "2026-08-30T00:00:00+00:00", "n")
        b = ("secret", "POST", "/api/v1/worker/jobs/claim", b"{}", "2026-08-30T00:00:00+00:00", "n")
        assert sign_request(*a) != sign_request(*b)


class TestTimestampFreshness:
    def test_current_timestamp_accepted(self) -> None:
        assert timestamp_is_fresh(datetime.now(UTC).isoformat()) is True

    def test_old_timestamp_rejected(self) -> None:
        stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        assert timestamp_is_fresh(stale) is False

    def test_future_timestamp_rejected(self) -> None:
        # Clock skew is tolerated; a wildly future timestamp is not.
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        assert timestamp_is_fresh(future) is False

    def test_garbage_timestamp_rejected(self) -> None:
        assert timestamp_is_fresh("not-a-timestamp") is False
