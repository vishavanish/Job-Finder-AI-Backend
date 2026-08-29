"""
app/models/application.py
----------------------------
Two tables:
  - Application: ONE row per (user, job_url) — current state. This is
    what GET /applications reads.
  - ApplicationStatusEvent: append-only history of every status change
    for that application (opened -> awaiting_review -> submitted ->
    rejected, etc.) — an audit trail.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Text, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import relationship

from app.core.db import Base
from app.models.user import User

class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("user_id", "job_url", name="uq_application_user_job"),
        Index("ix_application_user_status", "user_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)

    job_title = Column(String(500), default="")
    company = Column(String(255), default="", index=True)
    source = Column(String(100), default="")
    job_url = Column(Text, default="")

    status = Column(String(64), default="opened", nullable=False, index=True)
    status_source = Column(String(32), default="browser_apply", nullable=False)
    last_email_snippet = Column(Text, nullable=True)

    applied_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    events = relationship(
        "ApplicationStatusEvent",
        back_populates="application",
        order_by="ApplicationStatusEvent.created_at",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Application id={self.id} company={self.company!r} status={self.status!r}>"


class ApplicationStatusEvent(Base):
    """Append-only — never updated or deleted. One row per transition."""
    __tablename__ = "application_status_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id = Column(String(36), ForeignKey("applications.id"), nullable=False, index=True)

    status = Column(String(64), nullable=False)
    status_source = Column(String(32), default="browser_apply", nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    application = relationship("Application", back_populates="events")

    def __repr__(self) -> str:
        return f"<ApplicationStatusEvent app={self.application_id} status={self.status!r}>"