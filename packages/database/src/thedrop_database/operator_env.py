"""Load the operator's environment file for a hand-run command.

The services get their configuration from PM2, which parses
`~/.config/thedrop/thedrop.env` itself. A command typed into an SSH session gets
nothing, so every operational check had to be prefixed with

    set -a; . ~/.config/thedrop/thedrop.env; set +a

Forgetting it produced a connection attempt against a built-in localhost default and a
psycopg traceback that named nothing useful. It was forgotten repeatedly, including by
whoever was writing the instructions.

Deliberately NOT done on import of `thedrop_config`. The API and the worker already
have their environment, and a package that quietly reads a credential file when
imported would be a surprise in exactly the places surprises are least welcome. This is
an explicit call, made by the CLIs that are meant to be typed.

Existing variables always win: `setdefault`, never overwrite. Someone who passed
DATABASE_URL on the command line meant it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".config" / "thedrop" / "thedrop.env"


def _unquote(value: str) -> str:
    """Strip one matching pair of quotes, the way `source` would.

    Only a matching pair: a value that legitimately ends in a quote character keeps it,
    and `strip('"')` would have eaten it.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_operator_env(path: Path | None = None) -> Path | None:
    """Populate os.environ from the operator env file. Returns the file used, or None.

    A no-op when DATABASE_URL is already set, and a no-op when the file does not exist
    -- which is the normal case on a development machine, where the repository's own
    `.env` is what configuration comes from.
    """
    if os.environ.get("DATABASE_URL"):
        return None

    env_file = path or DEFAULT_ENV_FILE
    if not env_file.is_file():
        return None

    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            os.environ.setdefault(key, _unquote(value))
    return env_file
