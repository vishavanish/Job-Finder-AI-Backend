"""
app/services/scorer.py
------------------------
Keyword prefilter, then LLM job-fit scoring. Which model scores is decided
by the AI router (app/services/ai_router.py, CLAUDE.md 7.9): the user's
choice for the "score" task, the admin default, then the registry's
priority order (seeded as Gemini -> Groq -> Hugging Face, the old fixed
chain). A reply that can't be parsed counts as a failed call, so the
router moves on to the next model.
"""
from __future__ import annotations

import json
import re
from typing import Callable
import logging

from sqlalchemy.orm import Session

from app.services import ai_router

logger = logging.getLogger("job_finder_api.scorer")
NoOpProgress: Callable[[str], None] = lambda msg: None

LLM_SYSTEM_PROMPT_TEMPLATE = """You are an expert technical recruiter evaluating job fit.

    You will receive:
        1. A candidate résumé summary.
        2. Multiple job postings.

    For EACH job, calculate a job-fit score from 0 to 100.

    Evaluate using:
        - Skill and technology overlap: 50%
        - Seniority / experience match: 20%
        - Responsibilities match: 20%
        - Career-target alignment: 10%

    Candidate career targets:
        {career_targets}

    Important rules:
        - Do not give a high score merely because the job title looks good.
        - Missing important required skills should reduce the score.
        - Distinguish between required skills and optional skills.
        - Consider transferable backend/AI skills.
        - Be realistic about seniority.
        - Do not invent candidate experience.
        - Keep the reason to ONE sentence.

    Return ONLY valid JSON, no markdown, no explanation outside the JSON.
    Required format:
    [{{"index": 0, "score": 87, "reason": "Strong FastAPI and LangGraph overlap with relevant backend responsibilities."}}]
"""


def keyword_score(text: str, skills: list[str]) -> float:
    text = (text or "").lower()
    if not skills:
        return 0.0
    hits = sum(1 for skill in skills if re.search(rf"\b{re.escape(skill.lower())}\b", text))
    return round(hits / len(skills) * 100, 1)


def prefilter(
    jobs: list[dict],
    *,
    skills: list[str],
    keyword_prefilter_min_pct: float,
    llm_top_n_to_rank: int,
    progress: Callable[[str], None] = NoOpProgress,
) -> tuple[list[dict], int]:
    for job in jobs:
        job["keyword_score"] = keyword_score(job.get("description", ""), skills)

    survivors = [j for j in jobs if j["keyword_score"] >= keyword_prefilter_min_pct]
    survivors.sort(key=lambda x: x["keyword_score"], reverse=True)

    progress(f"keyword pre-filter: {len(survivors)}/{len(jobs)} jobs passed (>={keyword_prefilter_min_pct}%)")
    return survivors[:llm_top_n_to_rank], len(survivors)


def _build_user_prompt(jobs: list[dict], resume_summary: str) -> str:
    batch_desc = []
    for i, job in enumerate(jobs):
        snippet = (job.get("description", "") or "")[:1200]
        batch_desc.append(
            f"Index: {i}\nTitle: {job.get('title', '')}\n"
            f"Company: {job.get('company', '')}\nSource: {job.get('source', '')}\n"
            f"Description:\n{snippet}\n"
        )
    return f"CANDIDATE RESUME SUMMARY:\n{resume_summary}\n\nJOB POSTINGS:\n" + "\n".join(batch_desc)


def _score_entries(parsed) -> list | None:
    """The list of {index, score, reason} entries, or None. Accepts a bare
    list or an object wrapping one (some models add a top-level key)."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                return value
        if "index" in parsed:
            return [parsed]
    return None


def _parse_and_apply_scores(raw_text: str, jobs: list[dict], progress: Callable[[str], None]) -> bool:
    """Applies the model's scores to `jobs`. True only if at least one job
    got a score, so a reply the router can't use counts as a failed call
    and the next model is tried."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        entries = _score_entries(json.loads(cleaned))
    except json.JSONDecodeError:
        entries = None
    if entries is None:
        progress("could not parse LLM response as JSON")
        return False

    applied = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(jobs):
            continue
        try:
            score = float(entry.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        jobs[idx]["llm_score"] = max(0, min(100, score))
        jobs[idx]["llm_reason"] = str(entry.get("reason", ""))[:500]
        applied += 1
    return applied > 0


def _system_prompt(career_targets: list[str]) -> str:
    return LLM_SYSTEM_PROMPT_TEMPLATE.format(career_targets="\n        ".join(f"- {t}" for t in career_targets))


def _rank_via_router(
    db: Session, jobs: list[dict], *, user_id: str | None, resume_summary: str, career_targets: list[str],
    request_keys: dict[str, str] | None, ref: str | None, max_tokens: int, progress: Callable[[str], None],
) -> str | None:
    """Scores `jobs` in place with the first model that answers usefully.
    Returns the engine used ("provider:model"), or None if none could."""
    try:
        result = ai_router.complete(
            db, user_id=user_id, task="score", system=_system_prompt(career_targets),
            user_prompt=_build_user_prompt(jobs, resume_summary), json_mode=True, max_tokens=max_tokens,
            ref=ref, request_keys=request_keys,
            validate=lambda text: _parse_and_apply_scores(text, jobs, progress),
            progress=progress,
        )
    except ai_router.NoModelAvailable as e:
        progress(str(e))
        return None
    progress(f"{result.model.display_name} ranking succeeded")
    return result.engine


def llm_rank(
    db: Session,
    jobs: list[dict],
    *,
    user_id: str | None,
    resume_summary: str,
    career_targets: list[str],
    llm_min_score_to_keep: float,
    request_keys: dict[str, str] | None = None,
    ref: str | None = None,
    progress: Callable[[str], None] = NoOpProgress,
) -> tuple[list[dict], str | None]:
    if not jobs:
        return [], None

    engine_used = _rank_via_router(
        db, jobs, user_id=user_id, resume_summary=resume_summary, career_targets=career_targets,
        request_keys=request_keys, ref=ref, max_tokens=4000, progress=progress,
    )
    if engine_used is None:
        progress("every AI model failed or was unavailable — returning jobs unranked")
        for j in jobs:
            j.setdefault("llm_score", 0)
            j.setdefault("llm_reason", "LLM ranking unavailable this run.")
        return [], None

    ranked = [j for j in jobs if j.get("llm_score", 0) >= llm_min_score_to_keep]
    ranked.sort(key=lambda x: x["llm_score"], reverse=True)
    progress(f"{len(ranked)}/{len(jobs)} jobs scored >={llm_min_score_to_keep}")
    return ranked, engine_used


def score_one_job(
    db: Session, job: dict, *, user_id: str | None, resume_summary: str, career_targets: list[str],
    request_keys: dict[str, str] | None = None, ref: str | None = None,
) -> dict:
    """Scores exactly ONE job (the streaming pipeline scores per job so
    results appear live). Same prompt and parser as the batch path."""
    wrapper = [job]
    engine = _rank_via_router(
        db, wrapper, user_id=user_id, resume_summary=resume_summary, career_targets=career_targets,
        request_keys=request_keys, ref=ref, max_tokens=500, progress=NoOpProgress,
    )
    if engine is None:
        job.setdefault("llm_score", 0)
        job.setdefault("llm_reason", "LLM scoring unavailable for this job.")
    return job


def _tag_auto_apply_capability(jobs: list[dict]) -> None:
    from app.services.browser_apply import AUTO_APPLY_CAPABLE_SOURCES
    for job in jobs:
        job["auto_apply_capable"] = job.get("source") in AUTO_APPLY_CAPABLE_SOURCES


def score_jobs(
    db: Session,
    jobs: list[dict],
    *,
    user_id: str | None,
    resume_summary: str,
    skills: list[str],
    career_targets: list[str],
    keyword_prefilter_min_pct: float = 30,
    llm_top_n_to_rank: int = 40,
    llm_min_score_to_keep: float = 70,
    request_keys: dict[str, str] | None = None,
    ref: str | None = None,
    progress: Callable[[str], None] = NoOpProgress,
) -> dict:
    pre, total_prefiltered = prefilter(
        jobs, skills=skills, keyword_prefilter_min_pct=keyword_prefilter_min_pct,
        llm_top_n_to_rank=llm_top_n_to_rank, progress=progress,
    )
    ranked, engine_used = llm_rank(
        db, pre, user_id=user_id, resume_summary=resume_summary, career_targets=career_targets,
        llm_min_score_to_keep=llm_min_score_to_keep, request_keys=request_keys, ref=ref, progress=progress,
    )
    _tag_auto_apply_capability(ranked)  # lets the frontend show 1 vs 2 buttons per job
    return {
        "ranked_jobs": ranked,
        "total_input": len(jobs),
        "total_prefiltered": total_prefiltered,
        "total_ranked": len(ranked),
        "ranking_engine_used": engine_used,
    }
