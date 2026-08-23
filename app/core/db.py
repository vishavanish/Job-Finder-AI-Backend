"""
app/core/db.py
----------------
SQLAlchemy engine/session setup for the user-accounts database. Supports
either a local SQLite file (sqlite:///...) for local dev, or a remote
Turso database (sqlite+libsql://...) for persistent storage on ephemeral
hosts like Render, where local files get wiped on every redeploy/restart.

connect_args={"check_same_thread": False} is only valid/needed for local
SQLite-over-a-file connections — it's not applicable to the libsql remote
dialect, so it's applied conditionally based on which URL scheme is set.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import get_settings

settings = get_settings()

_is_local_sqlite_file = settings.AUTH_DATABASE_URL.startswith("sqlite:///")

engine = create_engine(
    settings.AUTH_DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_local_sqlite_file else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a session, always closes it after the
    request, even if an exception is raised mid-request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()