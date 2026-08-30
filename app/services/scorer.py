"""
app/services/scorer.py
------------------------
Ported from scorer.py. Candidate profile (resume/skills/career targets),
thresholds, model names, and even API keys can all be overridden per
request; if a request doesn't supply an API key, it falls back to the
server's env-configured key (app.core.config.Settings).

LLM RANKING FALLBACK CHAIN: Gemini -> Groq (openai/gpt-oss-120b) -> HF/Qwen3-8B.
Each stage only runs if its API key is present AND the previous stage
didn't already return a successfully parsed set of scores.
"""
from __future__ import annotations

import json
import re
from typing import Callable
import logging
from huggingface_hub import InferenceClient
from groq import Groq
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


def _parse_and_apply_scores(raw_text: str, jobs: list[dict], progress: Callable[[str], None]) -> bool:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        scores = json.loads(cleaned)
    except json.JSONDecodeError:
        progress("could not parse LLM response as JSON")
        return False

    for entry in scores:
        idx = entry.get("index")
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(jobs):
            continue
        try:
            score = float(entry.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        jobs[idx]["llm_score"] = max(0, min(100, score))
        jobs[idx]["llm_reason"] = entry.get("reason", "")
    return True


def _rank_with_gemini(
    jobs: list[dict], *, resume_summary: str, career_targets: list[str],
    gemini_model: str, gemini_api_key: str, progress: Callable[[str], None],
) -> bool:
    if not gemini_api_key:
        return False
    try:
        from google import genai
    except ImportError:
        progress("google-genai not installed — skipping Gemini, using Groq/HF only")
        return False

    client = genai.Client(api_key=gemini_api_key)
    system_prompt = LLM_SYSTEM_PROMPT_TEMPLATE.format(
        career_targets="\n        ".join(f"- {t}" for t in career_targets)
    )

    progress(f"trying Gemini ({gemini_model}) for {len(jobs)} jobs")
    try:
        response = client.models.generate_content(
            model=gemini_model,
            contents=_build_user_prompt(jobs, resume_summary),
            config={
                "system_instruction": system_prompt,
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )
        raw_text = response.text or ""
    except Exception as e:  # noqa: BLE001
        progress(f"Gemini call failed ({e}) — falling back to Groq/HF")
        return False

    if not raw_text.strip():
        progress("Gemini returned an empty response — falling back to Groq/HF")
        return False

    ok = _parse_and_apply_scores(raw_text, jobs, progress)
    if ok:
        progress("Gemini ranking succeeded")
    return ok


def _rank_with_groq(
    jobs: list[dict], *, resume_summary: str, career_targets: list[str],
    groq_model: str, groq_api_key: str, progress: Callable[[str], None],
) -> bool:
    if not groq_api_key:
        return False

    client = Groq(api_key=groq_api_key)
    system_prompt = LLM_SYSTEM_PROMPT_TEMPLATE.format(
        career_targets="\n        ".join(f"- {t}" for t in career_targets)
    )

    progress(f"trying Groq ({groq_model}) for {len(jobs)} jobs")
    try:
        response = client.chat.completions.create(
            model=groq_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _build_user_prompt(jobs, resume_summary)},
            ],
            temperature=0.1,
        )
        raw_text = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        progress(f"Groq call failed ({e}) — falling back to HF")
        return False

    if not raw_text.strip():
        progress("Groq returned an empty response — falling back to HF")
        return False

    ok = _parse_and_apply_scores(raw_text, jobs, progress)
    if ok:
        progress("Groq ranking succeeded")
    return ok


def _rank_with_hf(
    jobs: list[dict], *, resume_summary: str, career_targets: list[str],
    hf_model: str, hf_api_key: str, progress: Callable[[str], None],
) -> bool:
    if not hf_api_key:
        progress("no HF_API_KEY available — cannot use Qwen/HF fallback")
        return False

    hf_client = InferenceClient(api_key=hf_api_key)
    system_prompt = LLM_SYSTEM_PROMPT_TEMPLATE.format(
        career_targets="\n        ".join(f"- {t}" for t in career_targets)
    )

    progress(f"sending {len(jobs)} jobs to {hf_model} (Hugging Face) for ranking")
    try:
        response = hf_client.chat_completion(
            model=hf_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _build_user_prompt(jobs, resume_summary)},
            ],
            max_tokens=2000,
            temperature=0.1,
        )
        raw_text = response.choices[0].message.content.strip()
    except Exception as e:  # noqa: BLE001
        progress(f"Hugging Face inference error: {e}")
        return False

    ok = _parse_and_apply_scores(raw_text, jobs, progress)
    if ok:
        progress("Qwen/HF ranking succeeded")
    return ok


def llm_rank(
    jobs: list[dict],
    *,
    resume_summary: str,
    career_targets: list[str],
    llm_min_score_to_keep: float,
    gemini_model: str,
    groq_model: str,
    hf_model: str,
    gemini_api_key: str,
    groq_api_key: str,
    hf_api_key: str,
    progress: Callable[[str], None] = NoOpProgress,
) -> tuple[list[dict], str | None]:
    if not jobs:
        return [], None

    engine_used = None
    succeeded = _rank_with_gemini(
        jobs, resume_summary=resume_summary, career_targets=career_targets,
        gemini_model=gemini_model, gemini_api_key=gemini_api_key, progress=progress,
    )
    if succeeded:
        engine_used = f"gemini:{gemini_model}"
    else:
        succeeded = _rank_with_groq(
            jobs, resume_summary=resume_summary, career_targets=career_targets,
            groq_model=groq_model, groq_api_key=groq_api_key, progress=progress,
        )
        if succeeded:
            engine_used = f"groq:{groq_model}"
        else:
            succeeded = _rank_with_hf(
                jobs, resume_summary=resume_summary, career_targets=career_targets,
                hf_model=hf_model, hf_api_key=hf_api_key, progress=progress,
            )
            if succeeded:
                engine_used = f"hf:{hf_model}"

    if not succeeded:
        progress("Gemini, Groq, and Qwen/HF ranking all failed — returning jobs unranked")
        for j in jobs:
            j.setdefault("llm_score", 0)
            j.setdefault("llm_reason", "LLM ranking unavailable this run.")
        return [], None

    ranked = [j for j in jobs if j.get("llm_score", 0) >= llm_min_score_to_keep]
    ranked.sort(key=lambda x: x["llm_score"], reverse=True)
    progress(f"{len(ranked)}/{len(jobs)} jobs scored >={llm_min_score_to_keep}")
    return ranked, engine_used


def _tag_auto_apply_capability(jobs: list[dict]) -> None:
    from app.services.browser_apply import AUTO_APPLY_CAPABLE_SOURCES
    for job in jobs:
        job["auto_apply_capable"] = job.get("source") in AUTO_APPLY_CAPABLE_SOURCES


def score_jobs(
    jobs: list[dict],
    *,
    resume_summary: str,
    skills: list[str],
    career_targets: list[str],
    keyword_prefilter_min_pct: float = 30,
    llm_top_n_to_rank: int = 40,
    llm_min_score_to_keep: float = 70,
    gemini_model: str = "gemini-2.5-flash",
    groq_model: str = "openai/gpt-oss-120b",
    hf_model: str = "Qwen/Qwen3-8B",
    gemini_api_key: str = "",
    groq_api_key: str = "",
    hf_api_key: str = "",
    progress: Callable[[str], None] = NoOpProgress,
) -> dict:
    pre, total_prefiltered = prefilter(
        jobs, skills=skills, keyword_prefilter_min_pct=keyword_prefilter_min_pct,
        llm_top_n_to_rank=llm_top_n_to_rank, progress=progress,
    )
    ranked, engine_used = llm_rank(
        pre,
        resume_summary=resume_summary,
        career_targets=career_targets,
        llm_min_score_to_keep=llm_min_score_to_keep,
        gemini_model=gemini_model,
        groq_model=groq_model,
        hf_model=hf_model,
        gemini_api_key=gemini_api_key,
        groq_api_key=groq_api_key,
        hf_api_key=hf_api_key,
        progress=progress,
    )
    _tag_auto_apply_capability(ranked)  # NEW — lets the frontend show 1 vs 2 buttons per job
    return {
        "ranked_jobs": ranked,
        "total_input": len(jobs),
        "total_prefiltered": total_prefiltered,
        "total_ranked": len(ranked),
        "ranking_engine_used": engine_used,
    }