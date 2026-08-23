"""
app/services/job_sources.py
-----------------------------
Ported from the original job_sources.py. Behaviour is identical; every
value that was previously imported from config.py is now a function
argument, sourced from app.models.schemas.SearchRequest.
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from jobspy import scrape_jobs

SOURCE_DISPLAY_NAMES = {"linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri"}

NoOpProgress: Callable[[str], None] = lambda msg: None


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _pick(record: dict, *aliases: str, default: str = "") -> str:
    for alias in aliases:
        for key_variant in (alias, alias.lower(), alias.upper()):
            if key_variant in record:
                val = _as_string(record[key_variant])
                if val and val.lower() != "nan":
                    return val
    return default


def _build_location(record: dict) -> str:
    loc = _pick(record, "location")
    if loc:
        return loc
    city = _pick(record, "city")
    state = _pick(record, "state")
    return ", ".join(p for p in (city, state) if p)


def _run_jobspy(
    keyword: str,
    location: str,
    sources: list[str],
    *,
    results_per_keyword: int,
    max_age_hours: int,
    country_indeed: str,
    linkedin_fetch_description: bool,
    linkedin_max_retries: int,
    linkedin_retry_delay_sec: float,
    progress: Callable[[str], None],
) -> pd.DataFrame:
    attempt = 0
    max_attempts = linkedin_max_retries + 1 if "linkedin" in sources else 1
    df = pd.DataFrame()

    while attempt < max_attempts:
        attempt += 1
        progress(f"searching '{keyword}' in '{location}' across {sources} (attempt {attempt}/{max_attempts})")
        try:
            df = scrape_jobs(
                site_name=sources,
                search_term=keyword,
                location=location,
                results_wanted=results_per_keyword,
                hours_old=max_age_hours,
                country_indeed=country_indeed,
                linkedin_fetch_description=linkedin_fetch_description,
            )
        except Exception as e:  # noqa: BLE001
            progress(f"JobSpy search failed for '{keyword}': {e}")
            df = pd.DataFrame()

        got_linkedin_rows = (
            "linkedin" not in sources
            or (not df.empty and "site" in df.columns
                and (df["site"].astype(str).str.lower() == "linkedin").any())
        )

        if not df.empty and got_linkedin_rows:
            return df

        if attempt < max_attempts:
            wait = linkedin_retry_delay_sec + random.uniform(0, 5)
            progress(f"0 rows / possible rate limit — waiting {wait:.0f}s before retry")
            time.sleep(wait)

    return df


def _normalize(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []

    jobs = []
    for record in df.to_dict("records"):
        site = _pick(record, "site", "SITE").lower()
        source = SOURCE_DISPLAY_NAMES.get(site, site.title() or "Unknown")
        url = _pick(record, "job_url", "job_url_direct", "url", "JOB_URL")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")

        jobs.append({
            "source": source,
            "title": _pick(record, "title", "TITLE"),
            "company": _pick(record, "company", "COMPANY"),
            "location": _build_location(record),
            "url": url,
            "description": _pick(record, "description", "DESCRIPTION"),
            "posted": _pick(record, "date_posted", "DATE_POSTED", "posted"),
        })
    return jobs


def _validate_job(job: dict) -> tuple[bool, str]:
    if not job.get("title"):
        return False, "missing title"
    if not job.get("url"):
        return False, "missing URL"
    return True, ""


def _validate_jobs(jobs: list[dict], progress: Callable[[str], None]) -> list[dict]:
    valid, invalid = [], 0
    for job in jobs:
        ok, reason = _validate_job(job)
        if ok:
            valid.append(job)
        else:
            invalid += 1
    if invalid:
        progress(f"dropped {invalid} invalid job records")
    return valid


def _job_key(job: dict) -> str:
    url = job.get("url", "").strip().lower()
    if url:
        return url
    return "|".join([
        job.get("title", "").strip().lower(),
        job.get("company", "").strip().lower(),
        job.get("location", "").strip().lower(),
    ])


def _deduplicate_jobs(jobs: list[dict], progress: Callable[[str], None]) -> list[dict]:
    seen, unique = set(), []
    for job in jobs:
        key = _job_key(job)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(job)
    if len(jobs) - len(unique):
        progress(f"removed {len(jobs) - len(unique)} duplicate jobs")
    return unique


def scrape_all(
    keywords: list[str],
    locations: list[str],
    sources: list[str],
    *,
    results_per_keyword: int = 25,
    max_age_hours: int = 24,
    country_indeed: str = "India",
    linkedin_fetch_description: bool = True,
    search_delay_sec: float = 15,
    linkedin_max_retries: int = 2,
    linkedin_retry_delay_sec: float = 30,
    enable_naukri_auto: bool = False,
    manual_naukri_jobs: list[dict] | None = None,
    enable_naukri_apify: bool = False,
    apify_naukri_actor_id: str = "",
    apify_api_token: str = "",
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    sources = [s.strip().lower() for s in sources if s]
    locations = [loc.strip() for loc in locations if loc.strip()] or ["India"]

    auto_sources = [s for s in sources if s != "naukri" or enable_naukri_auto]
    if "naukri" in sources and not enable_naukri_auto:
        progress("Naukri auto-scraping is off — pass manual_naukri_jobs to include Naukri results")

    all_jobs: list[dict] = []
    total_searches = len(keywords) * len(locations)
    search_num = 0
    for keyword in keywords:
        for location in locations:
            search_num += 1
            if auto_sources:
                df = _run_jobspy(
                    keyword, location, auto_sources,
                    results_per_keyword=results_per_keyword,
                    max_age_hours=max_age_hours,
                    country_indeed=country_indeed,
                    linkedin_fetch_description=linkedin_fetch_description,
                    linkedin_max_retries=linkedin_max_retries,
                    linkedin_retry_delay_sec=linkedin_retry_delay_sec,
                    progress=progress,
                )
                all_jobs.extend(_normalize(df))
            if search_num < total_searches:
                delay = search_delay_sec + random.uniform(0, 3)
                progress(f"pausing {delay:.0f}s before next search")
                time.sleep(delay)

    if "naukri" in sources and manual_naukri_jobs:
        all_jobs.extend(manual_naukri_jobs)

    if "naukri" in sources and enable_naukri_apify:
        from app.services.naukri_apify_source import fetch_naukri_all_keywords
        all_jobs.extend(
            fetch_naukri_all_keywords(
                keywords, locations[0],
                apify_api_token=apify_api_token,
                apify_naukri_actor_id=apify_naukri_actor_id,
                results_per_keyword=results_per_keyword,
                progress=progress,
            )
        )

    progress(f"total raw jobs collected: {len(all_jobs)}")
    all_jobs = _validate_jobs(all_jobs, progress)
    all_jobs = _deduplicate_jobs(all_jobs, progress)
    progress(f"final usable jobs: {len(all_jobs)}")
    return all_jobs
