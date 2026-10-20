"""
app/models/email.py
---------------------
Gmail tracking tables (CLAUDE.md 7.8, P5):

  - EmailAccount: one connected Gmail address per row. The refresh token is
    sealed with key_store.seal (never stored or logged in plain text).
    status: active | needs_reconnect (the grant was revoked/expired) | paused.
  - EmailEvent: one job-related message. Only sender, subject, date, a short
    snippet and what was extracted from it — never the body.
    state: pending (no AI model yet; retried) | applied (changed an
    application) | needs_review (the user decides) | ignored | job_alert.
  - EmailJob: a job link parsed from a job-alert email (Naukri first); feeds
    the "Job alert emails" search source.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why

EVENT_STATES = ("pending", "applied", "needs_review", "ignored", "job_alert")
CATEGORIES = ("acknowledgement", "assessment", "interview_invite", "offer", "rejection", "job_alert", "other")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class EmailAccount(Base):
    __tablename__ = "email_accounts"
    __table_args__ = (UniqueConstraint("user_id", "gmail_address", name="uq_email_accounts_user_address"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    gmail_address = Column(String(255), nullable=False)
    token_sealed = Column(Text, nullable=False)
    scopes = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="active")
    last_history_id = Column(String(32), nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    next_sync_at = Column(DateTime, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_error = Column(String(300), nullable=True)
    connected_at = Column(DateTime, default=_now, nullable=False)


class EmailEvent(Base):
    __tablename__ = "email_events"
    __table_args__ = (UniqueConstraint("account_id", "message_id", name="uq_email_events_account_message"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(36), ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(String(64), nullable=False)
    thread_id = Column(String(64), nullable=True)
    sender = Column(String(320), nullable=False, default="")
    subject = Column(String(300), nullable=False, default="")
    snippet = Column(String(300), nullable=False, default="")
    received_at = Column(DateTime, nullable=False)
    category = Column(String(24), nullable=True)
    confidence = Column(Float, nullable=True)
    company = Column(String(255), nullable=True)
    role = Column(String(255), nullable=True)
    summary = Column(String(300), nullable=True)
    state = Column(String(16), nullable=False, default="pending")
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    applied_status = Column(String(32), nullable=True)
    match_score = Column(Integer, nullable=True)
    suggestions = Column(JSON, nullable=False, default=list)  # [{application_id, score, title, company}]
    engine = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class EmailJob(Base):
    __tablename__ = "email_jobs"
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_email_jobs_user_url"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(36), ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(64), nullable=False)
    title = Column(String(500), nullable=False)
    company = Column(String(255), nullable=False, default="")
    location = Column(String(255), nullable=False, default="")
    url = Column(Text, nullable=False)
    received_at = Column(DateTime, nullable=False)
