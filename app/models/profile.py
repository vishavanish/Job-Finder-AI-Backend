"""
app/models/profile.py
-----------------------
Who the user is and what they are looking for (CLAUDE.md 7.2, 7.3):

  - Profile: headline, summary, applicant info used by auto-apply, and
    work-authorization answers. One row per user.
  - Resume: every upload is a new version; exactly one is the master.
    Tailored variants (P4) point at their master through parent_id.
    `text` is the extracted text; `parsed` is the structured profile the
    AI produced and the user reviewed.
  - ResumeFile: the original file, envelope-encrypted (resume_store.py).
    Its own table so listing resumes never loads the file.
  - JobPreference: what kind of job the user wants. One row per user.
  - SearchProfile: a named Keywords & Skills set that drives search
    keywords, the keyword prefilter and the scoring context.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_profiles_user"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    headline = Column(String(255), nullable=True)
    summary = Column(Text, nullable=True)
    applicant_info = Column(JSON, nullable=False, default=dict)
    work_authorization = Column(JSON, nullable=False, default=list)  # [{question, answer}]
    eeo_encrypted = Column(Text, nullable=True)  # EeoAnswers JSON, envelope-encrypted (key_store.seal)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_resumes_user_version"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    is_master = Column(Boolean, nullable=False, default=False)
    parent_id = Column(String(36), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    label = Column(String(100), nullable=True)
    file_name = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    text = Column(Text, nullable=False, default="")
    parsed = Column(JSON, nullable=True)
    parse_status = Column(String(16), nullable=False, default="pending")  # unparsed|pending|parsed|reviewed|failed|no_text
    parse_error = Column(String(300), nullable=True)
    parsed_by = Column(String(300), nullable=True)  # "provider:model" that parsed it
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class ResumeFile(Base):
    __tablename__ = "resume_files"

    resume_id = Column(String(36), ForeignKey("resumes.id", ondelete="CASCADE"), primary_key=True)
    wrapped_key = Column(Text, nullable=False)  # per-file data key, encrypted with the master key
    ciphertext = Column(Text, nullable=False)   # the file, encrypted with the data key


class JobPreference(Base):
    __tablename__ = "job_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_job_preferences_user"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    titles = Column(JSON, nullable=False, default=list)
    seniority = Column(JSON, nullable=False, default=list)
    locations = Column(JSON, nullable=False, default=list)
    work_modes = Column(JSON, nullable=False, default=list)       # remote | hybrid | onsite
    job_types = Column(JSON, nullable=False, default=list)        # full_time | contract | ...
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_currency = Column(String(8), nullable=True)
    notice_period_days = Column(Integer, nullable=True)
    industries = Column(JSON, nullable=False, default=list)
    avoid_companies = Column(JSON, nullable=False, default=list)
    posted_within_hours = Column(Integer, nullable=True)
    languages = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class SearchProfile(Base):
    __tablename__ = "search_profiles"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_search_profiles_user_name"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    skills = Column(JSON, nullable=False, default=list)        # [{name, weight, synonyms, years}]
    titles = Column(JSON, nullable=False, default=list)        # target titles = search keywords
    must_have = Column(JSON, nullable=False, default=list)
    nice_to_have = Column(JSON, nullable=False, default=list)
    exclude = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
