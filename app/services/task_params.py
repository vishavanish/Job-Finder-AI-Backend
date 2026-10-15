"""
app/services/task_params.py
------------------------------
Turns validated request models into the plain-dict params Celery tasks
receive. One place, so /search, /score, /apply, /pipeline/run and
/pipeline/run/stream can't drift apart (they had: Groq settings were
dropped by every route).

Secrets never appear in the output: each user-supplied key becomes a
`*_key_ref` (see app/services/secret_refs.py) that the worker resolves.
Stored keys (key_store) are looked up by the worker from `user_id`.

Search params only list the sources this user may run right now (catalog,
user switches, Free tools only — app/services/provider_catalog.py);
`skipped_sources` says why the others were dropped.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.schemas import ApplyRequest, ScoreRequest, ScoreRequestPartial, SearchRequest
from app.models.user import User
from app.services import provider_catalog
from app.services.secret_refs import stash_secret_field


def build_search_params(req: SearchRequest, *, db: Session, user: User) -> dict:
    plan = provider_catalog.effective_sources(
        db, user, requested=list(req.sources), want_naukri_apify=req.enable_naukri_apify,
        has_apify_request_key=bool(req.apify_api_token),
    )
    if not plan.sources and not plan.naukri_apify and not (req.manual_naukri_jobs and "naukri" in req.sources):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "no_sources_available",
                    "message": "None of the selected job sources can run: " + "; ".join(plan.skipped),
                    "skipped": plan.skipped},
        )
    return {
        "user_id": user.id,
        "keywords": req.keywords,
        "locations": req.locations,
        "sources": plan.sources,
        "skipped_sources": plan.skipped,
        "results_per_keyword": req.results_per_keyword,
        "max_age_hours": req.max_age_hours,
        "country_indeed": req.country_indeed,
        "linkedin_fetch_description": req.linkedin_fetch_description,
        "search_delay_sec": req.search_delay_sec,
        "linkedin_max_retries": req.linkedin_max_retries,
        "linkedin_retry_delay_sec": req.linkedin_retry_delay_sec,
        "enable_naukri_auto": req.enable_naukri_auto,
        "manual_naukri_jobs": [j.model_dump() for j in req.manual_naukri_jobs],
        "enable_naukri_apify": plan.naukri_apify,
        "apify_naukri_actor_id": req.apify_naukri_actor_id,
        "apify_api_token_ref": stash_secret_field(req.apify_api_token),
    }


def build_score_params(req: ScoreRequest | ScoreRequestPartial, *, user_id: str) -> dict:
    """gemini/groq/hf model names are no longer used (the router picks the
    model); request keys still work as one-run keys for their provider."""
    return {
        "user_id": user_id,
        "resume_summary": req.resume_summary,
        "skills": req.skills,
        "career_targets": req.career_targets,
        "keyword_prefilter_min_pct": req.keyword_prefilter_min_pct,
        "llm_top_n_to_rank": req.llm_top_n_to_rank,
        "llm_min_score_to_keep": req.llm_min_score_to_keep,
        "gemini_api_key_ref": stash_secret_field(req.gemini_api_key),
        "groq_api_key_ref": stash_secret_field(req.groq_api_key),
        "hf_api_key_ref": stash_secret_field(req.hf_api_key),
    }


def build_apply_params(req: ApplyRequest, *, user_id: str) -> dict:
    # browser_profile_dir is never forwarded from the client — a
    # client-controlled path would let a caller make the worker read or
    # write arbitrary directories. The worker uses its own per-server dir.
    return {
        "applicant_info": req.applicant_info.model_dump(),
        "open_top_n": req.open_top_n,
        "auto_fill_easy_apply": req.auto_fill_easy_apply,
        "pause_between_tabs_sec": req.pause_between_tabs_sec,
        "browser_profile_dir": None,
        "headless": req.headless,
        "user_id": user_id,
        "require_auto_apply_capable": req.require_auto_apply_capable,
    }
