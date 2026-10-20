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


# ---------------------------------------------------------------- P4b: apply engine

# queued -> preparing -> awaiting_input <-> awaiting_review -> submitting -> submitted
# any active state -> cancelled | failed | needs_user (a hard stop: the user finishes on the real page)
RUN_ACTIVE_STATES = ("queued", "preparing", "awaiting_input", "awaiting_review", "submitting")
RUN_END_STATES = ("submitted", "failed", "cancelled", "needs_user", "done")


class ApplyRun(Base):
    __tablename__ = "apply_runs"
    __table_args__ = (UniqueConstraint("active_key", name="uq_apply_runs_active_key"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    job_key = Column(String(700), nullable=False)
    company_title = Column(String(800), nullable=False)  # normalised "company|title" for the cooldown check
    mode = Column(String(8), nullable=False)              # auto | fast
    level = Column(Integer, nullable=False)
    adapter = Column(String(16), nullable=False)          # greenhouse | lever | linkedin
    apply_url = Column(Text, nullable=False)
    state = Column(String(16), nullable=False, default="queued")
    active_key = Column(String(800), nullable=True)       # "user|job_key" while active, else NULL
    plan = Column(JSON, nullable=False, default=dict)     # {fields: [...], files: {...}}
    resume_id = Column(String(36), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    pack_id = Column(String(36), ForeignKey("apply_packs.id", ondelete="SET NULL"), nullable=True)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    message = Column(Text, nullable=True)                 # what the user should know / do next
    error = Column(Text, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class ApplyProof(Base):
    """Screenshot of the page after submitting (or where the run stopped),
    encrypted like resume files. Deleted after APPLY_PROOF_RETENTION_DAYS."""
    __tablename__ = "apply_proofs"

    id = Column(String(36), primary_key=True, default=_uuid)
    run_id = Column(String(36), ForeignKey("apply_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(16), nullable=False)             # confirmation | stopped
    confirmation_text = Column(Text, nullable=True)
    page_url = Column(Text, nullable=True)
    wrapped_key = Column(Text, nullable=False)
    ciphertext = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False, index=True)


class ApplySettings(Base):
    __tablename__ = "apply_settings"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    level = Column(Integer, nullable=False, default=1)    # 1 assist (no auto submit) | 2 review & submit
    risk_accepted_at = Column(DateTime, nullable=True)
    hours_start = Column(Integer, nullable=False, default=8)
    hours_end = Column(Integer, nullable=False, default=22)
    timezone = Column(String(64), nullable=False, default="Asia/Kolkata")
    paused = Column(Boolean, nullable=False, default=False)  # the user's own kill switch
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
