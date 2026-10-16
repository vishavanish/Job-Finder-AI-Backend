"""
app/models/profile_schemas.py
-------------------------------
Request/response models for profile, resumes, job preferences, search
profiles and the user's saved/hidden jobs (CLAUDE.md 7.2, 7.3, 7.5).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_terms(values: list[str], *, limit: int, max_len: int = 100) -> list[str]:
    """Trims, drops blanks and case-insensitive duplicates, keeps order."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        v = (v or "").strip()
        if not v or v.lower() in seen:
            continue
        if len(v) > max_len:
            raise ValueError(f"'{v[:20]}…' is longer than {max_len} characters")
        seen.add(v.lower())
        out.append(v)
    if len(out) > limit:
        raise ValueError(f"at most {limit} entries")
    return out


# ---------------------------------------------------------------- profile

class ApplicantInfoIn(BaseModel):
    """Deterministic answers auto-apply fills forms with (CLAUDE.md 7.2)."""
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field("", max_length=100)
    last_name: str = Field("", max_length=100)
    email: str = Field("", max_length=255)
    phone: str = Field("", max_length=50)
    city: str = Field("", max_length=100)
    country: str = Field("", max_length=100)
    linkedin_url: str = Field("", max_length=500)
    github_url: str = Field("", max_length=500)
    portfolio_url: str = Field("", max_length=500)
    notice_period_days: Optional[int] = Field(None, ge=0, le=365)
    current_ctc: str = Field("", max_length=50)
    expected_ctc: str = Field("", max_length=50)
    total_experience_years: Optional[float] = Field(None, ge=0, le=60)


class QA(BaseModel):
    question: str = Field(..., min_length=1, max_length=300)
    answer: str = Field(..., max_length=1000)


class ProfileIn(BaseModel):
    headline: Optional[str] = Field(None, max_length=255)
    summary: Optional[str] = Field(None, max_length=5000)
    applicant_info: ApplicantInfoIn = Field(default_factory=ApplicantInfoIn)
    work_authorization: list[QA] = Field(default_factory=list, max_length=30)


class ProfileOut(ProfileIn):
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------- resumes

class ExperienceItem(BaseModel):
    title: str = Field("", max_length=200)
    company: str = Field("", max_length=200)
    location: str = Field("", max_length=200)
    start: str = Field("", max_length=30)
    end: str = Field("", max_length=30)
    current: bool = False
    highlights: list[str] = Field(default_factory=list, max_length=30)


class EducationItem(BaseModel):
    school: str = Field("", max_length=200)
    degree: str = Field("", max_length=200)
    field: str = Field("", max_length=200)
    start: str = Field("", max_length=30)
    end: str = Field("", max_length=30)


class LinkItem(BaseModel):
    label: str = Field("", max_length=100)
    url: str = Field("", max_length=500)


class ParsedResume(BaseModel):
    """The structured profile the AI extracts and the user reviews."""
    name: str = Field("", max_length=200)
    email: str = Field("", max_length=255)
    phone: str = Field("", max_length=50)
    location: str = Field("", max_length=200)
    headline: str = Field("", max_length=255)
    summary: str = Field("", max_length=5000)
    total_experience_years: Optional[float] = Field(None, ge=0, le=60)
    experience: list[ExperienceItem] = Field(default_factory=list, max_length=40)
    education: list[EducationItem] = Field(default_factory=list, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=150)
    certifications: list[str] = Field(default_factory=list, max_length=50)
    languages: list[str] = Field(default_factory=list, max_length=20)
    links: list[LinkItem] = Field(default_factory=list, max_length=20)


ParseStatus = Literal["unparsed", "pending", "parsed", "reviewed", "failed", "no_text"]


class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int
    is_master: bool
    parent_id: Optional[str] = None
    label: Optional[str] = None
    file_name: str
    content_type: str
    size_bytes: int
    parse_status: ParseStatus
    parse_error: Optional[str] = None
    parsed_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ResumeDetail(ResumeOut):
    text: str
    parsed: Optional[ParsedResume] = None


class ResumeList(BaseModel):
    resumes: list[ResumeOut]
    master_id: Optional[str] = None


class ResumeUpdate(BaseModel):
    """The user's reviewed/edited parse result (and/or a new label)."""
    label: Optional[str] = Field(None, max_length=100)
    parsed: Optional[ParsedResume] = None


# ---------------------------------------------------------------- preferences

WorkMode = Literal["remote", "hybrid", "onsite"]
JobType = Literal["full_time", "part_time", "contract", "internship", "temporary", "freelance"]


class PreferencesIn(BaseModel):
    titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_modes: list[WorkMode] = Field(default_factory=list)
    job_types: list[JobType] = Field(default_factory=list)
    salary_min: Optional[int] = Field(None, ge=0)
    salary_max: Optional[int] = Field(None, ge=0)
    salary_currency: Optional[str] = Field(None, max_length=8)
    notice_period_days: Optional[int] = Field(None, ge=0, le=365)
    industries: list[str] = Field(default_factory=list)
    avoid_companies: list[str] = Field(default_factory=list)
    posted_within_hours: Optional[int] = Field(None, ge=1, le=720)
    languages: list[str] = Field(default_factory=list)

    @field_validator("titles", "seniority", "locations", "industries", "avoid_companies", "languages")
    @classmethod
    def _terms(cls, v: list[str]) -> list[str]:
        return _clean_terms(v, limit=50)

    @field_validator("salary_max")
    @classmethod
    def _range(cls, v, info):
        low = info.data.get("salary_min")
        if v is not None and low is not None and v < low:
            raise ValueError("salary_max must be at least salary_min")
        return v


class PreferencesOut(PreferencesIn):
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------- search profiles

SkillWeight = Literal["core", "strong", "familiar"]


class SkillIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    weight: SkillWeight = "strong"
    synonyms: list[str] = Field(default_factory=list)
    years: Optional[float] = Field(None, ge=0, le=60)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("skill name is empty")
        return v

    @field_validator("synonyms")
    @classmethod
    def _synonyms(cls, v: list[str]) -> list[str]:
        return _clean_terms(v, limit=10)


class SearchProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    is_default: bool = False
    skills: list[SkillIn] = Field(default_factory=list, max_length=100)
    titles: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is empty")
        return v

    @field_validator("titles")
    @classmethod
    def _titles(cls, v: list[str]) -> list[str]:
        return _clean_terms(v, limit=15)

    @field_validator("must_have", "nice_to_have", "exclude")
    @classmethod
    def _terms(cls, v: list[str]) -> list[str]:
        return _clean_terms(v, limit=50)

    @field_validator("skills")
    @classmethod
    def _unique_skills(cls, v: list[SkillIn]) -> list[SkillIn]:
        seen: set[str] = set()
        for s in v:
            if s.name.lower() in seen:
                raise ValueError(f"skill '{s.name}' is listed twice")
            seen.add(s.name.lower())
        return v


class SearchProfileOut(SearchProfileIn):
    id: str
    created_at: datetime
    updated_at: datetime


class SearchProfileList(BaseModel):
    profiles: list[SearchProfileOut]


class SeedFromResumeIn(BaseModel):
    name: str = Field("From my resume", min_length=1, max_length=100)
    resume_id: Optional[str] = Field(None, description="Defaults to the master resume")


class TitleSuggestions(BaseModel):
    titles: list[str]
    engine: Optional[str] = None


# ---------------------------------------------------------------- saved / hidden jobs

class UserJobOut(BaseModel):
    id: str = Field(..., description="Canonical job id (same as the pipeline feed's `id`)")
    title: str
    company: str
    location: str
    url: str
    source: str
    posted: str
    state: str
    keyword_score: Optional[float] = None
    llm_score: Optional[float] = None
    reason: Optional[str] = None
    sources: list[str] = Field(default_factory=list, description="Every source this job was found on")
    updated_at: datetime


class UserJobList(BaseModel):
    jobs: list[UserJobOut]
    total: int
