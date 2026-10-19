"""
app/models/apply_schemas.py
------------------------------
Request/response models for the Apply Center (CLAUDE.md 7.7, P4a): apply
options per job, prep packs and the answer bank.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

FieldType = Literal["text", "textarea", "select", "yes_no", "number"]


class ApplyOption(BaseModel):
    available: bool
    reason: Optional[str] = Field(None, description="Why the button is off; shown as its tooltip")


class ApplyOptions(BaseModel):
    """The four apply buttons for one job (P4 plan §2)."""
    adapter: Optional[Literal["greenhouse", "lever", "linkedin"]] = None
    manual: ApplyOption
    fast: ApplyOption
    auto: ApplyOption
    email: ApplyOption
    email_to: Optional[str] = Field(None, description="Address from the job post, when Email apply works")


# ---------------------------------------------------------------- prep pack

class PrepIn(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=36)
    tailor_resume: bool = True
    cover_letter: bool = True
    regenerate: bool = Field(False, description="Build a new pack even if one is ready")


class PrepPackOut(BaseModel):
    id: str
    job_id: str
    status: Literal["pending", "ready", "failed"]
    tailored_resume_id: Optional[str] = Field(None, description="Download with GET /resumes/{id}/file (DOCX)")
    cover_letter: Optional[str] = None
    email_to: Optional[str] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    mailto: Optional[str] = Field(None, description="mailto: link with the subject and body filled in")
    dropped: list[str] = Field(default_factory=list,
                               description="Things the AI wrote that are not in your resume, so they were removed")
    error: Optional[str] = None
    updated_at: datetime


# ---------------------------------------------------------------- answer bank

def _clean(value: str) -> str:
    return " ".join((value or "").split())


class AnswerIn(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    answer: str = Field(..., max_length=4000)
    field_type: FieldType = "text"

    _q = field_validator("question")(lambda cls, v: _clean(v))


class AnswerUpdate(BaseModel):
    answer: Optional[str] = Field(None, max_length=4000)
    approved: Optional[bool] = None


class AnswerOut(BaseModel):
    id: str
    question: str
    answer: str
    field_type: str
    approved: bool
    origin: Literal["user", "ai"]
    uses: int
    last_used_at: Optional[datetime] = None
    updated_at: datetime


class AnswerList(BaseModel):
    answers: list[AnswerOut]
    total: int
