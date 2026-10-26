"""
app/models/ats.py
-------------------
ATS Discovery (CLAUDE.md 7.6, P6):

  - AtsCompany: one company board on one ATS platform. Shared by all users
    (only public job lists are stored). `ref_key` = platform:slug[:host][:site]
    is the unique identity. origin: seed | pipeline | user | admin.
    The crawler claims rows whose next_crawl_at is due; `hot_until` makes a
    company refresh more often while people search for it.
  - AtsJob: one open job of a company. Short rows only (title, location,
    snippet ≤ 300) — full descriptions are fetched on demand. Jobs missing
    from a complete crawl get closed_at; closed jobs are deleted later.
    On Postgres a generated `tsv` column + GIN index serve the title search
    (migration 0011); SQLite (dev/tests) searches title_norm instead.
  - AtsPlatform: admin on/off + health per platform (greenhouse, lever, …).
  - AtsProbe: what the company resolver (QA3 §A) found for a company name seen
    in search results — the registry company, or nothing (retried after
    ATS_RESOLVER_RETRY_DAYS).
  - Target companies (QA4): AtsFollow (a user follows a company, or asked to
    follow a name we are still looking for), AtsFollowHit (a new matching
    role at a followed company, until seen / emailed), AtsFollowSettings
    (alert mode, titles, places per user). `AtsCompany.follower_count`
    makes followed companies refresh every ATS_FOLLOWED_REFRESH_HOURS.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why

ORIGINS = ("seed", "pipeline", "user", "admin", "resolver")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _uuid() -> str:
    return str(uuid.uuid4())


class AtsCompany(Base):
    __tablename__ = "ats_companies"
    __table_args__ = (UniqueConstraint("ref_key", name="uq_ats_companies_ref_key"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    ref_key = Column(String(400), nullable=False)
    platform = Column(String(24), nullable=False)
    slug = Column(String(128), nullable=False)
    host = Column(String(255), nullable=True)
    site = Column(String(128), nullable=True)
    name = Column(String(255), nullable=False)
    careers_url = Column(Text, nullable=False)
    origin = Column(String(16), nullable=False, default="seed")
    active = Column(Boolean, nullable=False, default=True)
    added_by_user_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    job_count = Column(Integer, nullable=False, default=0)
    etag = Column(String(255), nullable=True)
    content_hash = Column(String(64), nullable=True)
    last_crawled_at = Column(DateTime, nullable=True)
    last_ok_at = Column(DateTime, nullable=True)
    next_crawl_at = Column(DateTime, nullable=True)
    hot_until = Column(DateTime, nullable=True)
    fail_count = Column(Integer, nullable=False, default=0)
    gone_count = Column(Integer, nullable=False, default=0)
    last_error = Column(String(300), nullable=True)
    follower_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_now, nullable=False)


class AtsJob(Base):
    __tablename__ = "ats_jobs"
    __table_args__ = (UniqueConstraint("company_id", "external_id", name="uq_ats_jobs_company_external"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("ats_companies.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String(128), nullable=False)
    title = Column(String(500), nullable=False)
    title_norm = Column(String(500), nullable=False)
    location = Column(String(255), nullable=False, default="")
    country = Column(String(64), nullable=False, default="")
    remote = Column(Boolean, nullable=False, default=False)
    department = Column(String(255), nullable=False, default="")
    url = Column(Text, nullable=False)
    snippet = Column(String(300), nullable=False, default="")
    job_key = Column(String(700), nullable=False)
    content_hash = Column(String(64), nullable=False)
    posted_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, default=_now, nullable=False)
    last_seen_at = Column(DateTime, default=_now, nullable=False)
    closed_at = Column(DateTime, nullable=True)


class AtsPlatformSetting(Base):
    __tablename__ = "ats_platforms"

    platform = Column(String(24), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error = Column(String(300), nullable=True)


class AtsProbe(Base):
    __tablename__ = "ats_probes"

    name_norm = Column(String(255), primary_key=True)   # job_dedupe.normalize_company of the name searched
    name = Column(String(255), nullable=False)
    company_id = Column(String(36), ForeignKey("ats_companies.id", ondelete="SET NULL"), nullable=True)
    via = Column(String(16), nullable=True)               # website | slug (how it was found)
    checked_at = Column(DateTime, default=_now, nullable=False)


class AtsFollow(Base):
    __tablename__ = "ats_follows"
    __table_args__ = (UniqueConstraint("user_id", "company_id", name="uq_ats_follows_user_company"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id = Column(String(36), ForeignKey("ats_companies.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    name_norm = Column(String(255), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="following")   # following | searching | not_found
    created_at = Column(DateTime, default=_now, nullable=False)
    last_seen_at = Column(DateTime, default=_now, nullable=False)


class AtsFollowHit(Base):
    __tablename__ = "ats_follow_hits"
    __table_args__ = (UniqueConstraint("user_id", "ats_job_id", name="uq_ats_follow_hits_user_job"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id = Column(String(36), ForeignKey("ats_companies.id", ondelete="CASCADE"), nullable=False)
    ats_job_id = Column(String(36), ForeignKey("ats_jobs.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    emailed_at = Column(DateTime, nullable=True)
    seen_at = Column(DateTime, nullable=True)


class AtsFollowSettings(Base):
    __tablename__ = "ats_follow_settings"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    alert_mode = Column(String(16), nullable=False, default="daily")   # instant | daily | off
    titles = Column(JSON, nullable=False, default=list)                  # empty = default search profile titles
    places = Column(JSON, nullable=False, default=list)                  # empty = Profile places + Remote
    timezone = Column(String(64), nullable=False, default="Asia/Kolkata")
    last_digest_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
