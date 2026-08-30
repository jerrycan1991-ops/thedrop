"""Database layer for THE DROP.

This package is the *sole* owner of the schema (ADR-0006). The Next.js app never
opens a database connection; it reads through the FastAPI API.
"""

from thedrop_database.base import Base
from thedrop_database.session import (
    engine,
    get_session,
    session_scope,
    sessionmaker_for,
)

__all__ = [
    "Base",
    "engine",
    "get_session",
    "session_scope",
    "sessionmaker_for",
]
