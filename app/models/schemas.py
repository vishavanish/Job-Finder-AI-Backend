"""
app/models/schemas.py
----------------------
Every field that used to live as a hardcoded constant in config.py is now
part of one of these request models, with the old constant kept only as
the *default* so behaviour is unchanged unless the caller overrides it.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator

# ============================================================
# SHARED / CORE
# ============================================================

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    progress: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None


class Job(BaseModel):
    """Canonical job record shared across scraping / scoring / applying."""
    source: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    posted: str = ""

    keyword_score: Optional[float] = None
    llm_score: Optional[float] = None
    llm_reason: Optional[str] = None
    auto_apply_capable: Optional[bool] = Field(
        None, description="True if this job's source has a real auto-fill implementation "
        "(see app.services.browser_apply.AUTO_APPLY_CAPABLE_SOURCES). Set by the scoring "
        "step. Frontend uses this to decide whether to show one 'Apply' button or two "
        "('Apply' + 'Auto-Apply')."
    )

    model_config = {"extra": "allow"}
    
    
# ============================================================
# SEARCH (job_sources.py -> dynamic)
# ============================================================

class SearchRequest(BaseModel):
    keywords: list[str] = Field(
        default_factory=lambda: [
            "Senior Python Software Engineer",
            "AI Engineer",
            "LLM Application Developer",
            "Backend Developer Python FastAPI Django",
        ],
        min_length=1,
    )
    locations: list[str] = Field(default_factory=lambda: ["Mumbai, India", "Pune, India"])
    sources: list[Literal["linkedin", "indeed", "naukri"]] = Field(
        default_factory=lambda: ["linkedin", "indeed", "naukri"]
    )
    results_per_keyword: int = Field(25, ge=1, le=200)
    max_age_hours: int = Field(24, ge=1, le=720)
    country_indeed: str = "India"
    linkedin_fetch_description: bool = True

    search_delay_sec: float = Field(15, ge=0)
    linkedin_max_retries: int = Field(2, ge=0, le=10)
    linkedin_retry_delay_sec: float = Field(30, ge=0)

    enable_naukri_auto: bool = False
    manual_naukri_jobs: list[Job] = Field(
        default_factory=list,
        description="Jobs you pasted in manually (Naukri blocks automated scraping).",
    )

    enable_naukri_apify: bool = False
    apify_naukri_actor_id: str = ""
    apify_api_token: Optional[SecretStr] = Field(
        None, description="Overrides server APIFY_API_TOKEN for this call only."
    )


class SearchResult(BaseModel):
    jobs: list[Job]
    total_raw: int
    total_usable: int


# ============================================================
# COMPANY ATS (company_ats_sources.py -> dynamic)
# ============================================================

class CompanyAtsTarget(BaseModel):
    platform: Literal["greenhouse", "lever", "smartrecruiters", "custom"]
    slug: str
    custom_endpoint_url: Optional[str] = None
    custom_method: Literal["GET", "POST"] = "GET"
    custom_json_body: Optional[dict[str, Any]] = None

    # NEW — only used when platform == "custom" and the JSON-API probe fails
    custom_list_selector: Optional[str] = None     # CSS selector for each job card
    custom_title_selector: Optional[str] = None    # relative to list_selector
    custom_link_selector: Optional[str] = None      # relative to list_selector, reads href
    custom_location_selector: Optional[str] = None


class CompanyAtsRequest(BaseModel):
    targets: list[CompanyAtsTarget] = Field(
        default_factory=lambda: [
            CompanyAtsTarget(platform="smartrecruiters", slug="Atlassian"),
        ]
    )
    request_delay_sec: float = Field(2, ge=0)


class CompanyAtsResult(BaseModel):
    jobs: list[Job]
    total: int


# ============================================================
# SCORING (scorer.py -> dynamic)
# ============================================================

class ScoreRequest(BaseModel):
    jobs: list[Job] = Field(..., min_length=1)

    # candidate profile — was MY_SKILLS / MY_RESUME_SUMMARY
    resume_summary: str = Field(..., min_length=1)
    skills: list[str] = Field(..., min_length=1)
    career_targets: list[str] = Field(
        default_factory=lambda: ["AI Engineer", "LLM Application Developer", "Senior Backend Developer"]
    )

    # thresholds
    keyword_prefilter_min_pct: float = Field(30, ge=0, le=100)
    llm_top_n_to_rank: int = Field(40, ge=1, le=200)
    llm_min_score_to_keep: float = Field(70, ge=0, le=100)

    # model selection / overrides
    gemini_model: str = "gemini-2.5-flash"
    hf_model: str = "Qwen/Qwen3-8B"
    gemini_api_key: Optional[SecretStr] = Field(None, description="Overrides server GEMINI_API_KEY for this call only.")
    hf_api_key: Optional[SecretStr] = Field(None, description="Overrides server HF_API_KEY for this call only.")

class ScoreResult(BaseModel):
    ranked_jobs: list[Job]
    total_input: int
    total_prefiltered: int
    total_ranked: int
    ranking_engine_used: Optional[str] = None


# ============================================================
# APPLY (browser_apply.py -> dynamic)
# ============================================================

class ApplicantInfo(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str = ""


class ApplyRequest(BaseModel):
    jobs: list[Job] = Field(..., min_length=1)
    applicant_info: ApplicantInfo
    open_top_n: int = Field(10, ge=1, le=50)
    auto_fill_easy_apply: bool = True
    pause_between_tabs_sec: float = Field(2, ge=0)
    browser_profile_dir: Optional[str] = None
    headless: bool = Field(
        False, description="Run browser headless. Set True for server deployments with no display."
    )
    require_auto_apply_capable: bool = Field(
        True, description="If True (default), only opens/applies to jobs whose source has a real auto-fill implementation (currently LinkedIn Easy Apply). Set False to open top-N matches for manual review regardless of source."
    )

class ApplyLogEntry(BaseModel):
    date: str
    source: str
    title: str
    company: str
    url: str
    status: str


class ApplyResult(BaseModel):
    log: list[ApplyLogEntry]
    opened_count: int
    auto_applied_count: int = 0
    skipped_count: int = 0
    browser_open: bool = Field(
        False, description="True if a non-headless browser context is still open awaiting manual review/close."
    )


class PipelineRequest(BaseModel):
    search: SearchRequest
    score: "ScoreRequestPartial"
    apply: Optional[ApplyRequest] = None


class ScoreRequestPartial(BaseModel):
    """Same as ScoreRequest but without `jobs` — the pipeline supplies jobs
    from the search step automatically."""
    resume_summary: str
    skills: list[str] = Field(..., min_length=1)
    career_targets: list[str] = Field(
        default_factory=lambda: ["AI Engineer", "LLM Application Developer", "Senior Backend Developer"]
    )
    keyword_prefilter_min_pct: float = Field(30, ge=0, le=100)
    llm_top_n_to_rank: int = Field(40, ge=1, le=200)
    llm_min_score_to_keep: float = Field(70, ge=0, le=100)
    gemini_model: str = "gemini-2.5-flash"
    hf_model: str = "Qwen/Qwen3-8B"
    gemini_api_key: Optional[SecretStr] = None
    hf_api_key: Optional[SecretStr] = None


class ApplicationLogEntry(BaseModel):
    id: str
    date: str
    source: str
    title: str
    company: str
    url: str
    status: str = Field(
        ..., description=(
            "opened / easy_apply_form_filled_awaiting_review / open_failed:<error> / "
            "submitted / interview / rejected / offer / ghosted. Reflects the last "
            "known state — either what the /apply flow observed, a manual PATCH, "
            "or what an email parser detected."
        )
    )
    status_source: str = Field("", description="browser_apply | manual | email_parser")
    updated_at: Optional[datetime] = None


class ApplicationLogResult(BaseModel):
    applications: list[ApplicationLogEntry]
    total: int = Field(..., description="Total matching rows before pagination (limit/offset).")
    limit: int
    offset: int


class ApplicationStatusUpdate(BaseModel):
    status: Literal["opened", "submitted", "interview", "rejected", "offer", "ghosted"]
    
    
 # ============================================================
# AUTH (user accounts)
# ============================================================

class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

PipelineRequest.model_rebuild()
