"""
app/services/naukri_apify_source.py
-------------------------------------
Ported from naukri_apify_source.py. apify_api_token / actor id are now
passed in per-call (falling back to nothing — the caller/router decides
whether to use the server's env token or a request-supplied one).
"""
from __future__ import annotations

from typing import Callable

NoOpProgress: Callable[[str], None] = lambda msg: None


def _pick(record: dict, *aliases: str, default: str = "") -> str:
    for alias in aliases:
        val = record.get(alias)
        if val:
            return str(val).strip()
    return default


def fetch_naukri_via_apify(
    keyword: str,
    location: str,
    *,
    apify_api_token: str,
    apify_naukri_actor_id: str,
    results_per_keyword: int = 25,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    if not apify_api_token:
        progress("APIFY_API_TOKEN not set — skipping Naukri (Apify)")
        return []
    if not apify_naukri_actor_id:
        progress("apify_naukri_actor_id not provided — skipping Naukri (Apify)")
        return []

    from apify_client import ApifyClient
    client = ApifyClient(apify_api_token)

    progress(f"Naukri (Apify actor): searching '{keyword}' in '{location}'")
    try:
        run = client.actor(apify_naukri_actor_id).call(
            run_input={"searchKeywords": keyword, "location": location, "maxItems": results_per_keyword},
            timeout_secs=120,
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if any(t in msg for t in ("credit", "quota", "exceed", "limit", "payment")):
            progress(f"Apify usage credit appears exhausted — skipping Naukri ({e})")
        elif any(t in msg for t in ("unauthorized", "401", "token")):
            progress(f"Apify token invalid/expired — skipping Naukri ({e})")
        else:
            progress(f"Apify Naukri actor call failed — skipping Naukri ({e})")
        return []

    jobs = []
    for item in items:
        jobs.append({
            "source": "Naukri",
            "title": _pick(item, "title", "jobTitle"),
            "company": _pick(item, "companyName", "company"),
            "location": _pick(item, "location", "jobLocation"),
            "url": _pick(item, "url", "jdURL", "jobUrl"),
            "description": _pick(item, "description", "jobDescription", "jd"),
            "posted": _pick(item, "postedDate", "posted"),
        })
    progress(f"Naukri (Apify actor): {len(jobs)} jobs")
    return jobs


def fetch_naukri_all_keywords(
    keywords: list[str],
    location: str,
    *,
    apify_api_token: str,
    apify_naukri_actor_id: str,
    results_per_keyword: int = 25,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    all_jobs: list[dict] = []
    consecutive_empty = 0
    for keyword in keywords:
        jobs = fetch_naukri_via_apify(
            keyword, location,
            apify_api_token=apify_api_token,
            apify_naukri_actor_id=apify_naukri_actor_id,
            results_per_keyword=results_per_keyword,
            progress=progress,
        )
        if not jobs:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                progress("Naukri (Apify) failed twice in a row — stopping remaining keywords")
                break
        else:
            consecutive_empty = 0
        all_jobs.extend(jobs)
    return all_jobs
