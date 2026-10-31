"""
app/models/extension.py
-------------------------
Browser extension "Job Finder capture" (P8a E1):

  - ExtensionToken: a capture-only token the extension holds after pairing. Only its SHA-256 is stored;
    revoking it (or the user's account being disabled) stops the extension at once.
  - CapturedJob: a job the user saved from a page they opened (Naukri, Foundit, Glassdoor, LinkedIn,
    Indeed). Read by the "Saved from your browser" search source; kept CAPTURE_RETENTION_DAYS.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ExtensionToken(Base):
    __tablename__ = "extension_tokens"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    name = Column(String(120), nullable=False, default="Browser extension")
    created_at = Column(DateTime, nullable=False, default=_now)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class CapturedJob(Base):
    __tablename__ = "captured_jobs"
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_captured_jobs_user_url"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    site = Column(String(32), nullable=False)
    source = Column(String(64), nullable=False)
    title = Column(String(500), nullable=False)
    company = Column(String(255), nullable=False, default="")
    location = Column(String(255), nullable=False, default="")
    url = Column(Text, nullable=False)
    description = Column(Text, nullable=False, default="")
    posted = Column(String(64), nullable=False, default="")
    captured_at = Column(DateTime, nullable=False, default=_now, index=True)
