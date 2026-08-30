"""Engine and session management.

Pool sizes are deliberately small. Postgres runs on the same 8 GB box as everything
else with ``max_connections=60``; the whole application must fit comfortably inside
that (DATABASE.md §12).
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from thedrop_config import get_settings


@lru_cache(maxsize=1)
def _build_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        str(settings.database_url),
        # 10 base + 5 overflow per API process, well inside max_connections=60.
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        future=True,
    )


def engine() -> Engine:
    return _build_engine()


@lru_cache(maxsize=1)
def sessionmaker_for() -> sessionmaker[Session]:
    return sessionmaker(bind=_build_engine(), expire_on_commit=False, autoflush=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency. One session per request, always closed."""
    session = sessionmaker_for()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts and Celery tasks."""
    session = sessionmaker_for()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
