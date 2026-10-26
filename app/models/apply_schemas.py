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
    adapter: Optional[Literal["greenhouse", "lever", "linkedin", "generic"]] = Field(
        None, description="generic = the AI form reader (beta; turn it on in Apply settings)")
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
    error: Optional[str] = Field(None, description="Short message for people when part of the pack failed")
    error_detail: Optional[str] = Field(None, description="Which AI models were tried and why each failed")
    steps: dict[str, str] = Field(default_factory=dict,
                                  description="Per part (resume, cover_letter): running, done, failed or skipped")
    typical_seconds: Optional[int] = Field(None, description="How long packs usually take (median of the last 20)")
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


# ---------------------------------------------------------------- P4b: apply runs and settings

RunState = Literal["queued", "preparing", "awaiting_input", "awaiting_review", "submitting", "submitted", "failed",
                   "cancelled", "needs_user", "done"]

RISK_NOTICE = ("Auto apply fills and submits applications on company career sites (Greenhouse, Lever) for you, "
               "only after you review and approve each one. Some sites limit automated applications, answers can "
               "still be wrong, and a submitted application can't be taken back. You stay responsible for every "
               "application sent in your name. LinkedIn applications are never submitted by the app.")
GENERIC_NOTICE = ("The AI form reader (beta) fills application forms on other career sites (Ashby, SmartRecruiters, "
                  "Workable and more). It is less reliable than the Greenhouse and Lever support: it can misread a "
                  "question or a page. It never signs in or creates accounts, every application waits for your "
                  "review, and pages it can't handle are handed back to you. It may use a paid image-reading AI "
                  "model for hard pages, within a per-application limit.")


class ApplySettingsIn(BaseModel):
    level: Optional[int] = Field(None, ge=1, le=2, description="1 = Assist (nothing submitted), 2 = review & submit")
    accept_risk_notice: Optional[bool] = None
    hours_start: Optional[int] = Field(None, ge=0, le=23)
    hours_end: Optional[int] = Field(None, ge=1, le=24)
    timezone: Optional[str] = Field(None, max_length=64)
    paused: Optional[bool] = Field(None, description="Your own off switch; pausing cancels active runs")
    allow_generic: Optional[bool] = Field(None, description="Use the AI form reader (beta) on other career sites; "
                                                            "turning it off cancels its active runs")


class ApplySettingsOut(BaseModel):
    level: int
    max_level: int = Field(..., description="Highest level your account allows (set by an admin)")
    risk_notice: str = RISK_NOTICE
    allow_generic: bool = False
    generic_notice: str = GENERIC_NOTICE
    generic_submit_enabled: bool = Field(False, description="False = the AI form reader is in test mode: runs "
                                                            "are prepared for review but never submitted")
    risk_accepted_at: Optional[datetime] = None
    hours_start: int
    hours_end: int
    timezone: str
    paused: bool
    usage_today: dict
    caps: dict


class RunIn(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=36)
    mode: Literal["auto", "fast"] = "auto"


class RunField(BaseModel):
    key: str
    label: str
    type: str
    options: list[str] = Field(default_factory=list)
    required: bool
    value: Optional[str] = None
    source: str = Field(..., description="profile | eeo | answer_bank | ai | user | none")
    approved: bool
    needs_input: bool


class RunOut(BaseModel):
    id: str
    job_id: str
    title: str
    company: str
    mode: Literal["auto", "fast"]
    level: int
    adapter: str
    apply_url: str
    state: RunState
    message: Optional[str] = None
    fields: list[RunField] = Field(default_factory=list)
    files: dict = Field(default_factory=dict, description="{field key: resume | cover_letter}")
    resume_id: Optional[str] = None
    pack_id: Optional[str] = None
    application_id: Optional[str] = None
    proofs: int = 0
    steps: list[dict] = Field(default_factory=list, description="AI form reader: the form pages read "
                                                                "({page, url, keys, files})")
    vision: Optional[dict] = Field(None, description="AI form reader: screenshot-fallback use "
                                                     "({steps, cost_usd, map_calls, log})")
    approved_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RunList(BaseModel):
    runs: list[RunOut]
    total: int


class RunAnswers(BaseModel):
    answers: dict[str, str] = Field(..., description="{field key: answer}", max_length=100)


class RunDone(BaseModel):
    submitted: bool = Field(True, description="false = you decided not to apply")


class ProofOut(BaseModel):
    id: str
    kind: Literal["confirmation", "stopped"]
    confirmation_text: Optional[str] = None
    page_url: Optional[str] = None
    created_at: datetime
