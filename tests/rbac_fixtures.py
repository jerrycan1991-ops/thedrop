"""Disposable RBAC fixture accounts.

Creates one user per role plus a multi-role user, so authorization can be tested
without touching the real admin account and without a second person's credentials
ever entering the repository.

Design rules:
  * Passwords are generated per test run with `secrets` and exist only in memory.
    Nothing is committed, and nothing is written to a baseline file.
  * Emails use a reserved prefix so cleanup is unambiguous and can never match a real
    account. `.test` and `.local` are NOT used -- email-validator rejects reserved
    TLDs, which is how the original admin account ended up unusable.
  * Teardown deletes the users; `user_roles` rows go with them via ON DELETE CASCADE.
    Redis sessions are cleared separately because Redis has no foreign keys.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from thedrop_database import session_scope
from thedrop_database.models import Role, User

#: Every fixture account starts with this. Cleanup deletes exactly these.
EMAIL_PREFIX = "zz-rbac-"
EMAIL_DOMAIN = "thedrop.channel"

#: role key -> role slugs held. The multi-role user exists to make role ORDERING
#: observable; with a single role the array order is unverifiable.
FIXTURE_ROLES: dict[str, list[str]] = {
    "admin": ["admin"],
    "editor": ["editor"],
    "analyst": ["analyst"],
    "viewer": ["viewer"],
    # Deliberately assigned in NON-alphabetical order so a test that passes only
    # because insertion happened to match the expected output cannot pass by luck.
    "multi": ["viewer", "editor", "analyst"],
}


@dataclass
class FixtureUser:
    key: str
    email: str
    password: str
    roles: list[str]
    user_id: int | None = None

    @property
    def expected_roles(self) -> list[str]:
        """Canonical ordering: alphabetical by slug. See models/auth.py."""
        return sorted(self.roles)


@dataclass
class FixtureSet:
    users: dict[str, FixtureUser] = field(default_factory=dict)

    def __getitem__(self, key: str) -> FixtureUser:
        return self.users[key]


def _email(key: str) -> str:
    return f"{EMAIL_PREFIX}{key}@{EMAIL_DOMAIN}"


def create_fixture_users() -> FixtureSet:
    """Create every fixture account. Idempotent: removes leftovers first."""
    # Imported lazily so this module does not require the API package unless used.
    from app.security import hash_password

    cleanup_fixture_users()

    fixtures = FixtureSet()
    password = secrets.token_urlsafe(24)

    with session_scope() as db:
        roles_by_slug = {r.slug: r for r in db.scalars(select(Role))}

        for key, slugs in FIXTURE_ROLES.items():
            missing = [s for s in slugs if s not in roles_by_slug]
            if missing:
                raise RuntimeError(f"roles missing from the database: {missing}")

            user = User(
                email=_email(key),
                password_hash=hash_password(password),
                display_name=f"RBAC fixture ({key})",
                is_active=True,
            )
            for slug in slugs:
                user.roles.append(roles_by_slug[slug])

            db.add(user)
            db.flush()

            fixtures.users[key] = FixtureUser(
                key=key,
                email=user.email,
                password=password,
                roles=list(slugs),
                user_id=user.id,
            )

    return fixtures


def cleanup_fixture_users() -> int:
    """Delete every fixture account. `user_roles` cascades."""
    with session_scope() as db:
        result = db.execute(
            delete(User).where(User.email.like(f"{EMAIL_PREFIX}%"))
        )
        return result.rowcount or 0


def cleanup_fixture_sessions(redis_client) -> int:
    """Remove Redis sessions belonging to fixture users.

    Redis has no foreign keys, so deleting the users leaves their sessions behind
    until the idle TTL expires.
    """
    import json

    removed = 0
    for key in redis_client.scan_iter("session:*"):
        raw = redis_client.get(key)
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if str(payload.get("email", "")).startswith(EMAIL_PREFIX):
            redis_client.delete(key)
            removed += 1
    return removed
