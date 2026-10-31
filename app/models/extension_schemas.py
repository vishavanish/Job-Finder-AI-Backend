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
