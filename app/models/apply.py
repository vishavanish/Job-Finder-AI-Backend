"""
app/models/apply.py
---------------------
Apply Center tables (CLAUDE.md 7.7, P4a):

  - AnswerBankEntry: a screening question the user has met, with the
    answer to reuse. origin "user" (typed or approved by the user) or "ai"
    (an AI draft). Only approved answers may be used to submit a form
    without the user looking at it.
  - ApplyPack: the prep pack for one job — tailored resume (a child
    Resume of the master), cover letter, email draft. One row per
    (user, job); rebuilt when the master resume changes (master_hash).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why

PACK_STATUSES = ("pending", "ready", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class AnswerBankEntry(Base):
    __tablename__ = "answer_bank"
    __table_args__ = (UniqueConstraint("user_id", "question_hash", name="uq_answer_bank_user_question"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    question_hash = Column(String(64), nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False, default="")
    field_type = Column(String(16), nullable=False, default="text")  # text | textarea | select | yes_no | number
    approved = Column(Boolean, nullable=False, default=False)
    origin = Column(String(8), nullable=False, default="user")     # user | ai
    uses = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class ApplyPack(Base):
    __tablename__ = "apply_packs"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_apply_packs_user_job"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    master_resume_id = Column(String(36), ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False)
    master_hash = Column(String(64), nullable=False)  # sha256 of the master's parsed JSON
    status = Column(String(16), nullable=False, default="pending")
    want_resume = Column(Boolean, nullable=False, default=True)
    want_cover_letter = Column(Boolean, nullable=False, default=True)
    tailored_resume_id = Column(String(36), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    cover_letter = Column(Text, nullable=True)
    email_to = Column(String(255), nullable=True)
    email_subject = Column(String(300), nullable=True)
    email_body = Column(Text, nullable=True)
    dropped = Column(JSON, nullable=False, default=list)  # AI claims removed by the fact check
    engine = Column(String(300), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
