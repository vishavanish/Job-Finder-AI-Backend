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

from app.models.schemas import (
    ApplyRequest, PipelineRequest, ScoreRequest, ScoreRequestPartial, ScoreThresholds, SearchRequest,
)
from app.models.user import User
from app.services import provider_catalog, search_profiles
from app.services.secret_refs import stash_secret_field


def client_info(request) -> tuple[str, str]:
    """(IP, User-Agent) of the API caller, for sources that require them."""
    return (request.client.host if request.client else ""), request.headers.get("user-agent", "")


def _remote_ok(locations: list[str], prefs) -> bool:
    """Remote-only feeds run when the search is for remote work."""
    if any("remote" in (loc or "").lower() for loc in locations):
        return True
    return bool(prefs and "remote" in [str(m).lower() for m in (prefs.work_modes or [])])


def build_search_params(req: SearchRequest, *, db: Session, user: User,
                        client: tuple[str, str] | None = None) -> dict:
    """`client` = (IP, User-Agent) of the caller; only Careerjet needs it."""
    requested = list(req.sources) if req.sources is not None else None
    plan = provider_catalog.effective_sources(
        db, user, requested=requested, want_naukri_apify=req.enable_naukri_apify,
        has_apify_request_key=bool(req.apify_api_token),
        remote_ok=_remote_ok(req.locations, search_profiles.preferences(db, user.id)),
    )
    if requested is None and req.manual_naukri_jobs:
        plan.sources.append("naukri")
    if not plan.sources and not plan.naukri_apify:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "no_sources_available",
                    "message": "None of the selected job sources can run: " + "; ".join(plan.skipped),
                    "skipped": plan.skipped},
        )
    if req.enable_naukri_auto:
        plan.skipped.append("Naukri automatic search: no longer available (Naukri requires a captcha) — "
                            "use manual import or Naukri via Apify")
    params = {
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
        "enable_naukri_auto": False,  # deprecated; JobSpy's Naukri search is blocked
        "manual_naukri_jobs": [j.model_dump() for j in req.manual_naukri_jobs],
        "enable_naukri_apify": plan.naukri_apify,
        "apify_naukri_actor_id": req.apify_naukri_actor_id,
        "apify_api_token_ref": stash_secret_field(req.apify_api_token),
    }
    if "careerjet" in plan.sources and client:
        params["client_ip"], params["client_user_agent"] = client[0][:64], client[1][:300]
    return params


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


def build_pipeline_params(req: PipelineRequest, *, db: Session, user: User,
                          client: tuple[str, str] | None = None) -> tuple[dict, dict]:
    """(search_params, score_params) for /pipeline/run and /pipeline/run/stream.

    Profile mode fills keywords, skills and context from the saved search
    profile, preferences and master resume; fields the caller set
    explicitly in `search` still win. Both modes add the filters and the
    seen-job settings the keyword tasks use (CLAUDE.md 7.3, 7.5)."""
    prefs = search_profiles.preferences(db, user.id)
    search = req.search
    filters: dict = {"avoid_companies": search_profiles.avoided_companies(prefs)}

    if req.search_profile_id:
        profile = search_profiles.get_owned(db, user.id, req.search_profile_id)
        explicit = search.model_fields_set
        updates: dict = {}
        if "keywords" not in explicit:
            titles = profile.titles or (prefs.titles if prefs else []) or []
            if not titles:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
                    "code": "search_profile_no_titles",
                    "message": "Add at least one job title to this search profile."})
            updates["keywords"] = list(titles)
        if "locations" not in explicit and prefs and prefs.locations:
            updates["locations"] = list(prefs.locations)
        if "max_age_hours" not in explicit and prefs and prefs.posted_within_hours:
            updates["max_age_hours"] = prefs.posted_within_hours
        search = search.model_copy(update=updates)

        if req.score is not None:
            score = build_score_params(req.score, user_id=user.id)
            score["resume_summary"] = req.score.resume_summary
        else:
            thresholds = req.thresholds or ScoreThresholds()
            score = {"user_id": user.id, **thresholds.model_dump(),
                     "resume_summary": search_profiles.resume_context(db, user.id)}
        skills = profile.skills or []
        score["skills"] = [s["name"] for s in skills] + list(profile.nice_to_have or [])
        score["skill_terms"] = search_profiles.skill_terms(skills, profile.nice_to_have or [])
        score["career_targets"] = list(profile.titles or [])
        filters.update(must_have=list(profile.must_have or []), exclude=list(profile.exclude or []))
        score["search_profile_id"] = profile.id
    else:
        score = build_score_params(req.score, user_id=user.id)

    score["filters"] = filters
    score["include_seen"] = req.include_seen
    score["profile_hash"] = search_profiles.profile_hash(score)
    return build_search_params(search, db=db, user=user, client=client), score


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
