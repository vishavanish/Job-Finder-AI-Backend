"""
app/models/alerts.py
----------------------
Saved job alerts (P3.2): a search profile run once a day at the user's
chosen local hour; new matches above `min_score` are kept in a run row and
emailed as a digest (to the sign-in email or a connected Gmail address,
`email_to`). `next_run_at` is UTC and doubles as the claim — the
scheduler only runs an alert whose next_run_at it managed to move forward.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class JobAlert(Base):
    __tablename__ = "job_alerts"
    __table_args__ = (Index("ix_job_alerts_due", "enabled", "next_run_at"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    search_profile_id = Column(String(36), ForeignKey("search_profiles.id", ondelete="CASCADE"), nullable=False)
    hour_local = Column(Integer, nullable=False, default=9)
    timezone = Column(String(64), nullable=False, default="Asia/Kolkata")
    min_score = Column(Integer, nullable=False, default=75)
    email_digest = Column(Boolean, nullable=False, default=True)
    email_to = Column(String(255), nullable=True)  # NULL = sign-in email; else a connected Gmail address
    enabled = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class JobAlertRun(Base):
    __tablename__ = "job_alert_runs"

    id = Column(String(36), primary_key=True, default=_uuid)
    alert_id = Column(String(36), ForeignKey("job_alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    trigger = Column(String(16), nullable=False, default="schedule")  # schedule | manual
    status = Column(String(16), nullable=False, default="running")    # running | done | failed | skipped
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    counts = Column(JSON, nullable=False, default=dict)
    job_ids = Column(JSON, nullable=False, default=list)
    emailed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
