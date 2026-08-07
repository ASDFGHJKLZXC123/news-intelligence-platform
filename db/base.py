"""SQLAlchemy engine, session factory, and declarative base.

Stage 1 defines no business tables (those arrive in Stage 2). This module only wires
the connection machinery so migrations, health checks, and the pgvector smoke test
have a shared engine/session to use.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from packages.config.settings import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models (populated from Stage 2 onward)."""


_settings = get_settings()

# ``pool_pre_ping`` keeps health checks honest against recycled/stale connections.
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_timeout=_settings.database_pool_timeout_seconds,
    connect_args={
        "connect_timeout": _settings.database_connect_timeout_seconds,
        "options": f"-c statement_timeout={_settings.database_statement_timeout_ms}",
    },
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session():
    """Yield a session and always close it. Use as a FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
