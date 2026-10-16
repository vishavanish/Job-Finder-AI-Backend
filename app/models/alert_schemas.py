"""Request/response models for /alerts (P3.2)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings
from app.models.profile_schemas import UserJobOut


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    search_profile_id: str = Field(..., min_length=1, max_length=36)
    hour_local: int = Field(9, ge=0, le=23, description="Local hour the alert runs at")
    timezone: str = Field("Asia/Kolkata", max_length=64)
    min_score: int = Field(default_factory=lambda: get_settings().JOB_ALERT_DEFAULT_MIN_SCORE, ge=0, le=100)
    email_digest: bool = True
    enabled: bool = True

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from app.services.job_alerts import valid_timezone
        return valid_timezone(value)


class AlertOut(AlertIn):
    id: str
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = Field(None, description="UTC")
    created_at: datetime


class AlertList(BaseModel):
    alerts: list[AlertOut]
    max_alerts: int


class AlertRunOut(BaseModel):
    id: str
    alert_id: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    counts: dict = Field(default_factory=dict)
    new_jobs: int = 0
    emailed_at: Optional[datetime] = None
    error: Optional[str] = None


class AlertRunList(BaseModel):
    runs: list[AlertRunOut]


class AlertRunJobs(BaseModel):
    run: AlertRunOut
    jobs: list[UserJobOut]
