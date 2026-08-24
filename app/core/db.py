"""
app/core/db.py
----------------
SQLAlchemy engine/session setup for the app's single Postgres database
(Supabase). Both user accounts (app/models/user.py) and job/application
data (app/models/application.py) share this one engine/Base now — there
used to be a separate SQLite file per concern; Supabase's free tier gives
you one Postgres database, so everything lives there.

pool_pre_ping=True: Supabase (and any managed Postgres) will silently
drop idle connections after a timeout. Without pre-ping, the FIRST query
on a stale connection raises a raw psycopg OperationalError instead of
SQLAlchemy transparently reconnecting. Cheap per-checkout SELECT 1, worth
it to avoid random 500s after any idle period.

pool_size/max_overflow are kept small deliberately: if you're using
Supabase's pooler (pgbouncer, transaction mode) this doesn't matter much
since pgbouncer is doing the real pooling upstream. If you're using the
DIRECT connection string instead, Supabase's free tier caps you at ~60
total connections across everything (API process + every Celery worker),
so a small per-process pool matters a lot there.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=1800,  # recycle connections every 30 min, belt-and-suspenders with pre_ping
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