"""
app/models/jobs.py
--------------------
Canonical jobs shared by all users, and each user's relation to them
(CLAUDE.md 7.5):

  - Job: one row per real-world job, keyed by job_dedupe.job_key. Holds
    the best copy found so far (LinkedIn first, else the longest
    description) — see app/services/job_store.py.
  - JobSourceSeen: every source a job was found on, with its URL.
  - UserJob: what a user has done with a job and its score for them.
    state: seen (shown in a feed) | filtered (scored below the user's
    threshold, never shown) | saved | hidden | applied.
    profile_hash says which search profile + resume the score belongs
    to, so a filtered job is re-scored when the profile changes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why

USER_JOB_STATES = ("seen", "filtered", "saved", "hidden", "applied")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("job_key", name="uq_jobs_job_key"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    job_key = Column(String(700), nullable=False)
    title = Column(String(500), nullable=False, default="")
    company = Column(String(255), nullable=False, default="")
    location = Column(String(255), nullable=False, default="")
    url = Column(Text, nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    posted = Column(String(64), nullable=False, default="")
    best_source = Column(String(100), nullable=False, default="")
    best_source_rank = Column(Integer, nullable=False, default=2)  # source_info.source_rank; lower is better
    first_seen_at = Column(DateTime, default=_now, nullable=False)
    last_seen_at = Column(DateTime, default=_now, nullable=False)


class JobSourceSeen(Base):
    __tablename__ = "job_sources_seen"
    __table_args__ = (UniqueConstraint("job_id", "source", name="uq_job_sources_seen_job_source"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(100), nullable=False)
    url = Column(Text, nullable=False, default="")
    first_seen_at = Column(DateTime, default=_now, nullable=False)
    last_seen_at = Column(DateTime, default=_now, nullable=False)


class UserJob(Base):
    __tablename__ = "user_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_user_jobs_user_job"),
        Index("ix_user_jobs_user_state", "user_id", "state"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    state = Column(String(16), nullable=False, default="seen")
    keyword_score = Column(Float, nullable=True)
    llm_score = Column(Float, nullable=True)
    reason = Column(Text, nullable=True)
    profile_hash = Column(String(32), nullable=True)
    search_profile_id = Column(String(36), nullable=True)
    last_run_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
