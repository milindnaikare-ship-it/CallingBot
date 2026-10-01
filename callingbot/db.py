"""Database engine and session management (SQLAlchemy 2.x).

SQLite is the default for development; set ``DATABASE_URL`` to a PostgreSQL URL
(``postgresql+psycopg://...``) for production.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def configure_engine(url: str | None = None) -> Engine:
    """(Re)create the global engine. Tests call this with ``sqlite://`` for an in-memory DB."""
    global _engine, _SessionLocal
    if url is None:
        from callingbot.settings import get_settings

        url = get_settings().database_url
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def init_db() -> None:
    """Create all tables (idempotent). Use Alembic migrations once the schema stabilises."""
    import callingbot.models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(get_engine())


def new_session() -> Session:
    if _SessionLocal is None:
        configure_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts/background jobs: commit on success, rollback on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Route handlers are responsible for calling ``session.commit()``."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
