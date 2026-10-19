"""
app/models/email_schemas.py
------------------------------
Request/response models for Gmail tracking and the tracker additions (P5).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

EventCategory = Literal["acknowledgement", "assessment", "interview_invite", "offer", "rejection", "job_alert", "other"]


class GmailConnectOut(BaseModel):
    auth_url: str = Field(..., description="Open this in the browser; Google sends the user back to /gmail/callback")


class GmailAccountOut(BaseModel):
    id: str
    gmail_address: str
    status: Literal["active", "needs_reconnect", "paused"]
    last_synced_at: Optional[datetime] = None
    next_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    connected_at: datetime
    reading: str = Field("Read-only access. Only job-related emails are looked at; the app stores the sender, "
                         "subject, date and a short snippet — never the full email.")


class GmailAccountList(BaseModel):
    accounts: list[GmailAccountOut]
    configured: bool = Field(..., description="False when the server has no Gmail OAuth client set up")


class Suggestion(BaseModel):
    application_id: str
    score: int
    title: str
    company: str


class EmailEventOut(BaseModel):
    id: str
    account_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime
    category: Optional[EventCategory] = None
    confidence: Optional[float] = None
    company: Optional[str] = None
    role: Optional[str] = None
    summary: Optional[str] = None
    state: Literal["pending", "applied", "needs_review", "ignored", "job_alert"]
    application_id: Optional[str] = None
    applied_status: Optional[str] = None
    match_score: Optional[int] = None
    suggestions: list[Suggestion] = Field(default_factory=list)


class EmailEventList(BaseModel):
    events: list[EmailEventOut]
    total: int


class ResolveIn(BaseModel):
    action: Literal["confirm", "change", "ignore"]
    application_id: Optional[str] = Field(None, description="For change (or to confirm a different suggestion)")
    category: Optional[Literal["acknowledgement", "assessment", "interview_invite", "offer", "rejection"]] = None


class SyncResult(BaseModel):
    account_id: str
    new: int = 0
    job_related: int = 0
    applied: int = 0
    needs_review: int = 0
    job_alerts: int = 0
    pending: int = 0
    error: Optional[str] = None
    skipped: Optional[str] = None


# ---------------------------------------------------------------- tracker

class TimelineEvent(BaseModel):
    status: str
    status_source: str
    note: Optional[str] = None
    created_at: datetime
    email: Optional[dict] = Field(None, description="{sender, subject, received_at} of the email that caused it")


class ApplicationTimeline(BaseModel):
    application_id: str
    events: list[TimelineEvent]


class TrackerStats(BaseModel):
    total: int
    this_week: int
    funnel: dict = Field(..., description="applied, acknowledged, assessment, interview, offer (reached at least)")
    rejected: int
    by_status: dict
    by_source: dict
    needs_review: int
