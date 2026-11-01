"""Request/response models for the browser extension routes (P8a E1)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class PairCodeOut(BaseModel):
    code: str = Field(..., description="6 digits to type into the extension; single use")
    expires_in: int = Field(..., description="Seconds until the code stops working")


class TokenIn(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)
    name: Optional[str] = Field(None, max_length=120, description="e.g. 'Chrome on my laptop'")


class TokenOut(BaseModel):
    token: str = Field(..., description="Capture-only token; the extension stores it. Shown once.")
    id: str
    email: str


class ConnectionOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: Optional[datetime] = None


class ConnectionsOut(BaseModel):
    connections: list[ConnectionOut]
    saved_total: int
    saved_today: int


class CapturedJobIn(BaseModel):
    title: str = Field(..., max_length=1000)
    url: str = Field(..., max_length=2000)
    company: str = Field("", max_length=1000)
    location: str = Field("", max_length=1000)
    posted: str = Field("", max_length=200)
    description: str = Field("", max_length=20000)


class CaptureIn(BaseModel):
    site: str = Field(..., max_length=32, description="naukri | foundit | glassdoor | linkedin | indeed")
    page_url: str = Field(..., max_length=2000)
    jobs: list[CapturedJobIn] = Field(..., max_length=100)


class CaptureOut(BaseModel):
    saved: int
    updated: int
    skipped: int
    total: int


class ExtensionMeOut(BaseModel):
    email: str
    saved_total: int
    saved_today: int


# ---------------------------------------------------------------- Fast apply in the browser (P8b E3a)

class AssistRunOut(BaseModel):
    run_id: Optional[str] = Field(None, description="Your Fast apply run for this LinkedIn job; null = none")
    title: str = ""
    company: str = ""


class AssistFieldIn(BaseModel):
    key: str = Field(..., max_length=200)
    label: str = Field("", max_length=2000)
    type: str = Field("text", max_length=16, description="text | textarea | number | select | radio | checkbox | "
                                                         "combobox | file")
    options: list[str] = Field(default_factory=list, max_length=200)
    required: bool = False


class AssistFillIn(BaseModel):
    fields: list[AssistFieldIn] = Field(..., max_length=60)


class AssistAnswerOut(BaseModel):
    key: str
    value: Optional[str] = None
    source: str = Field(..., description="profile | eeo | answer_bank | ai | user | none")
    needs_input: bool


class AssistFillOut(BaseModel):
    answers: list[AssistAnswerOut]
    files: dict[str, str] = Field(default_factory=dict, description="{field key: resume}")


class AssistRememberIn(BaseModel):
    key: str = Field(..., max_length=200)
    value: str = Field(..., max_length=2000)


class AssistResultIn(BaseModel):
    outcome: str = Field(..., pattern="^(submitted|closed)$",
                         description="submitted = LinkedIn confirmed it; closed = the tab closed first")


class AssistResultOut(BaseModel):
    run_id: str
    state: str


class AssistPageAnswerIn(BaseModel):
    """An answer typed in the panel on a page with no run (E3b): saved to the answer bank."""
    label: str = Field(..., max_length=2000)
    value: str = Field(..., max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=200)
