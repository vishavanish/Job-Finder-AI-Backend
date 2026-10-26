"""Request/response models for ATS Discovery (P6): app/api/routes/ats.py."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class AtsJobOut(BaseModel):
    id: str
    title: str
    location: str
    remote: bool
    department: str
    url: str
    posted_at: datetime | None = None
    first_seen_at: datetime | None = None
    applied: bool = False
    # QA2 §4: the same role posted for other places, shown as "+N locations" on one row
    more_locations: list[str] = []


class AtsJobDetail(AtsJobOut):
    company_id: str
    company: str
    platform: str
    description: str
    job_id: str | None = None   # canonical jobs.id once tracked


class AtsCompanyGroup(BaseModel):
    company_id: str
    name: str
    platform: str
    platform_name: str
    careers_url: str
    open_jobs: int
    matching_jobs: int
    top_jobs: list[AtsJobOut]
    locations: list[str]
    newest_posted_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    followed: bool = False            # QA4: the user follows this company


class AtsRegistryStats(BaseModel):
    companies: int
    jobs: int
    last_refresh_at: datetime | None = None
    by_platform: dict[str, int] = {}


class AtsSearchResult(BaseModel):
    query: str
    expansions: list[str]
    expansion_pending: bool
    groups: list[AtsCompanyGroup]
    total_companies: int
    total_jobs: int
    page: int
    page_size: int
    registry: AtsRegistryStats
    topup_task_id: str | None = None


class AtsCompanyOut(BaseModel):
    id: str
    name: str
    platform: str
    platform_name: str
    slug: str
    careers_url: str
    origin: str
    active: bool
    job_count: int
    last_crawled_at: datetime | None = None
    last_ok_at: datetime | None = None
    next_crawl_at: datetime | None = None
    last_error: str | None = None
    created: bool = False


class AtsCompanyJobs(BaseModel):
    company: AtsCompanyOut
    jobs: list[AtsJobOut]
    followed: bool = False            # QA4


class AtsCompanyIn(BaseModel):
    """A careers / job link (preferred), or platform + company id."""
    url: str | None = Field(default=None, max_length=2000)
    platform: str | None = Field(default=None, max_length=24)
    slug: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _one_way(self):
        if not self.url and not (self.platform and self.slug):
            raise ValueError("give a careers page link, or platform and slug")
        return self


class AtsPlatformOut(BaseModel):
    platform: str
    name: str
    enabled: bool
    companies: int
    consecutive_failures: int
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None


class AtsPlatformUpdate(BaseModel):
    enabled: bool


class AtsCompanyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    active: bool | None = None


class AtsHealth(BaseModel):
    registry: AtsRegistryStats
    index_limit: int
    platforms: list[AtsPlatformOut]
    failing_companies: list[AtsCompanyOut]
    switched_off: int


class AtsTracked(BaseModel):
    job_id: str


# ---------------------------------------------------------------- Target companies (QA4)

class FollowedCompany(BaseModel):
    id: str
    status: str                       # following | searching | not_found
    name: str
    company_id: str | None = None
    platform: str | None = None
    platform_name: str | None = None
    careers_url: str | None = None
    open_jobs: int = 0
    open_matching: int = 0
    new_count: int = 0
    latest_posted_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    followed_at: datetime


class FollowSettingsOut(BaseModel):
    alert_mode: str
    titles: list[str]
    places: list[str]
    timezone: str
    default_titles: list[str]
    default_places: list[str]


class FollowSettingsIn(BaseModel):
    alert_mode: str | None = None
    titles: list[str] | None = Field(default=None, max_length=10)
    places: list[str] | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=64)


class FollowingOut(BaseModel):
    companies: list[FollowedCompany]
    limit: int
    settings: FollowSettingsOut


class FollowByNameIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class FollowByNameOut(BaseModel):
    status: str                       # following | searching
    follow: FollowedCompany | None = None
    task_id: str | None = None


class NewRole(BaseModel):
    id: str
    job_id: str
    title: str
    location: str
    remote: bool
    url: str
    company_id: str
    company: str
    platform: str
    posted_at: datetime | None = None
    found_at: datetime
    seen: bool


class NewRolesOut(BaseModel):
    roles: list[NewRole]


class SeenIn(BaseModel):
    company_id: str | None = None


class SuggestedCompany(BaseModel):
    company_id: str
    name: str
    platform: str
    platform_name: str
    open_matching: int
    reason: str


class SuggestionsOut(BaseModel):
    companies: list[SuggestedCompany]
