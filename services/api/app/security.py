"""Password hashing, session tokens and worker credentials.

Nothing here reads a hardcoded secret. Everything derives from settings.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# 64 MiB / 3 iterations / 4 lanes. Comfortable on a 4-core VPS while still being
# expensive enough to make offline cracking impractical.
_hasher = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=4)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # malformed hash, unknown algorithm, etc.
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_worker_token() -> str:
    """Worker tokens are generated, never chosen. 48 bytes of entropy."""
    return secrets.token_urlsafe(48)


def token_digest(token: str) -> bytes:
    """Tokens are stored hashed, so a database leak yields no working credential."""
    return hashlib.sha256(token.encode()).digest()


def constant_time_equals(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def sign_request(
    secret: str, method: str, path: str, body: bytes, timestamp: str, nonce: str
) -> str:
    """HMAC over the whole request, so a captured worker call cannot be replayed."""
    digest = hashlib.sha256(body).hexdigest()
    message = f"{method.upper()}\n{path}\n{digest}\n{timestamp}\n{nonce}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def timestamp_is_fresh(timestamp: str, tolerance_seconds: int = 120) -> bool:
    try:
        sent = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return abs(datetime.now(UTC) - sent) <= timedelta(seconds=tolerance_seconds)
