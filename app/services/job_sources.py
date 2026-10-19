"""
app/services/job_sources.py
-----------------------------
Runs one search across every requested job source (CLAUDE.md 7.4) and
returns validated, de-duplicated jobs in the plain-dict shape used
everywhere else. Source kinds and adapters: app/providers/job_sources/.

  jobspy   LinkedIn, Indeed, Glassdoor, Google Jobs — one JobSpy call per
           keyword x location, paced (search_delay_sec) with LinkedIn retries
  keyword  Adzuna, Jooble, Careerjet — free keys resolved by the caller's
           `key_lookup` (never passed in params), daily caps per user
  feed     remote feeds — cached in Redis, filtered by keyword, region and age
  manual   Naukri jobs the user pasted; Apify for Naukri when enabled

`key_lookup(scope, platform_only=False)` returns a key_store.ResolvedKey or None.
`on_source_result(key, error)` reports each source's outcome so the caller
can record health: error=None when it answered, the reason when it failed.
A failing source never fails the search.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable

import pandas as pd
from jobspy import scrape_jobs

from app.providers.job_sources import aggregators, feeds, registry
from app.providers.job_sources.base import (
    SourceError, SourceQuery, geo_allows, is_recent, matches_keyword,
)
from app.core.config import get_settings
from app.services import job_dedupe, source_limits

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
) -> tuple[pd.DataFrame, str | None]:
    """(rows, last error). JobSpy logs most per-site failures itself and
    returns no rows, so an empty frame is not treated as an error."""
    attempt = 0
    error = None
    extra = {}
    if "google" in sources:
        when = "since yesterday" if max_age_hours <= 24 else "in the last week"
        extra["google_search_term"] = f"{keyword} jobs near {location} {when}"
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
                **extra,
            )
            error = None
        except Exception as e:  # noqa: BLE001
            error = f"JobSpy: {type(e).__name__}"
            progress(f"JobSpy search failed for '{keyword}': {e}")
            df = pd.DataFrame()

        got_linkedin_rows = (
            "linkedin" not in sources
            or (not df.empty and "site" in df.columns
                and (df["site"].astype(str).str.lower() == "linkedin").any())
        )

        if not df.empty and got_linkedin_rows:
            return df, None

        if attempt < max_attempts:
            wait = linkedin_retry_delay_sec + random.uniform(0, 5)
            progress(f"0 rows / possible rate limit — waiting {wait:.0f}s before retry")
            time.sleep(wait)

    return df, error


def _normalize(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []

    jobs = []
    for record in df.to_dict("records"):
        site = _pick(record, "site", "SITE").lower()
        spec = registry.by_jobspy_site(site)
        source = spec.name if spec else (site.title() or "Unknown")
        url = _pick(record, "job_url", "job_url_direct", "url", "JOB_URL")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        # The company's own apply page behind a board posting (often Greenhouse/Lever) — P4a adapters need it.
        direct = _pick(record, "job_url_direct")
        apply_url = direct if direct.startswith(("http://", "https://")) and direct != url else ""

        jobs.append({
            "source": source,
            "title": _pick(record, "title", "TITLE"),
            "company": _pick(record, "company", "COMPANY"),
            "location": _build_location(record),
            "url": url,
            "description": _pick(record, "description", "DESCRIPTION"),
            "posted": _pick(record, "date_posted", "DATE_POSTED", "posted"),
            "apply_url": apply_url,
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


def _deduplicate_jobs(jobs: list[dict], progress: Callable[[str], None]) -> list[dict]:
    """Same job from several sources/searches -> one copy, LinkedIn
    preferred. Rules live in app/services/job_dedupe.py."""
    unique = job_dedupe.dedupe(jobs)
    if len(jobs) - len(unique):
        progress(f"removed {len(jobs) - len(unique)} duplicate jobs")
    return unique


class _Outcomes:
    """Per-source result of one scrape_all call, for source health."""

    def __init__(self) -> None:
        self.ok: set[str] = set()
        self.errors: dict[str, str] = {}

    def success(self, key: str) -> None:
        self.ok.add(key)

    def failure(self, key: str, error: str) -> None:
        self.errors[key] = error[:300]

    def report(self, callback: Callable[[str, str | None], None] | None) -> None:
        if callback is None:
            return
        for key in self.ok:
            callback(key, None)
        for key, error in self.errors.items():
            if key not in self.ok:  # answered at least once in this search -> healthy
                callback(key, error)


def _run_keyword_api(key: str, query: SourceQuery, *, user_id: str | None, key_lookup, outcomes: _Outcomes,
                     progress: Callable[[str], None]) -> list[dict]:
    """Runs a keyed API. A source with several key scopes (Google Jobs)
    tries them in order and stops at the first that returns jobs."""
    from app.services.provider_catalog import user_key_only

    spec = registry.get(key)
    own_key_only = user_key_only(key)
    tried, last_error = [], None
    for scope in spec.key_scopes:
        resolved = key_lookup(scope) if key_lookup else None
        if not resolved or (own_key_only and resolved.source != "byok"):
            continue
        label = spec.name if len(spec.key_scopes) == 1 else f"{spec.name} ({scope})"
        if not source_limits.take_quota(f"{key}:{scope}", user_id, resolved.source):
            progress(f"{label}: today's search limit reached — skipped until tomorrow")
            continue
        tried.append(scope)
        try:
            jobs = aggregators.KEYWORD_APIS[scope](query, resolved.value)
        except SourceError as e:
            last_error = str(e)
            progress(f"{label} failed: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — never echo the exception text: it may contain the key
            last_error = f"{label}: {type(e).__name__}"
            progress(f"{label} failed: {type(e).__name__}")
            continue
        outcomes.success(key)
        progress(f"{label}: {len(jobs)} jobs for '{query.keyword}' in '{query.location}'")
        if jobs or scope == spec.key_scopes[-1]:
            return jobs
    if not tried and last_error is None:
        progress(f"{spec.name}: no usable API key — skipped")
    elif last_error and key not in outcomes.ok:
        outcomes.failure(key, last_error)
    return []


def _run_feed(key: str, keywords: list[str], *, country: str, max_age_hours: int, limit: int,
              outcomes: _Outcomes, progress: Callable[[str], None], key_lookup=None) -> list[dict]:
    spec = registry.get(key)
    fetch = feeds.FEEDS[key]
    if spec.platform_key_scope and key_lookup:
        # The feed download is shared by all users, so only a platform/.env key is used.
        resolved = key_lookup(spec.platform_key_scope, platform_only=True)
        if resolved:
            fetch = lambda: feeds.FEEDS[key](resolved.value)  # noqa: E731
    ttl = get_settings().HN_FEED_CACHE_TTL_SECONDS if key == "hn_hiring" else None
    try:
        feed = source_limits.cached_feed(key, fetch, ttl=ttl)
    except source_limits.FeedUnavailable as e:
        progress(f"{spec.name}: skipped this time ({e})")
        return []
    except SourceError as e:
        outcomes.failure(key, str(e))
        progress(f"{spec.name} failed: {e}")
        return []
    except Exception as e:  # noqa: BLE001
        outcomes.failure(key, f"{spec.name}: {type(e).__name__}")
        progress(f"{spec.name} failed: {type(e).__name__}")
        return []
    if feed:
        outcomes.success(key)
    matched = []
    for job in feed:
        if not geo_allows(job.get("_geo", ""), country) or not is_recent(job.get("posted", ""), max_age_hours):
            continue
        if any(matches_keyword(job, kw, job.get("_tags", "")) for kw in keywords):
            matched.append({k: v for k, v in job.items() if not k.startswith("_")})
    progress(f"{spec.name}: {len(matched)} matching remote jobs (of {len(feed)} in the feed)")
    return matched[:limit]


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
    inbox_jobs: list[dict] | None = None,
    enable_naukri_apify: bool = False,
    apify_naukri_actor_id: str = "",
    apify_api_token: str = "",
    user_id: str | None = None,
    client_ip: str = "",
    client_user_agent: str = "",
    key_lookup: Callable[[str], Any] | None = None,
    on_source_result: Callable[[str, str | None], None] | None = None,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    sources = [s.strip().lower() for s in sources if s]
    locations = [loc.strip() for loc in locations if loc.strip()] or ["India"]
    if enable_naukri_auto:
        # Deprecated: JobSpy's Naukri search now always gets "recaptcha required" (tested 2026-10-16).
        progress("automatic Naukri search is no longer available (Naukri requires a captcha) — "
                 "use manual import or Naukri via Apify")

    jobspy_sites = [registry.get(s).jobspy_site for s in sources if registry.kind(s) == "jobspy"]
    keyword_apis = [s for s in sources if registry.kind(s) == "keyword"]
    feed_keys = [s for s in sources if registry.kind(s) == "feed"]
    outcomes = _Outcomes()

    all_jobs: list[dict] = []
    total_searches = len(keywords) * len(locations)
    search_num = 0
    for keyword in keywords:
        for location in locations:
            search_num += 1
            if jobspy_sites:
                df, error = _run_jobspy(
                    keyword, location, jobspy_sites,
                    results_per_keyword=results_per_keyword,
                    max_age_hours=max_age_hours,
                    country_indeed=country_indeed,
                    linkedin_fetch_description=linkedin_fetch_description,
                    linkedin_max_retries=linkedin_max_retries,
                    linkedin_retry_delay_sec=linkedin_retry_delay_sec,
                    progress=progress,
                )
                rows = _normalize(df)
                for site in jobspy_sites:
                    key = registry.by_jobspy_site(site).key
                    if any(registry.key_for_name(j["source"]) == key for j in rows):
                        outcomes.success(key)
                    elif error:
                        outcomes.failure(key, error)
                all_jobs.extend(rows)
            query = SourceQuery(keyword=keyword, location=location, country=country_indeed,
                                max_age_hours=max_age_hours, results_wanted=results_per_keyword,
                                client_ip=client_ip, client_user_agent=client_user_agent)
            for key in keyword_apis:
                all_jobs.extend(_run_keyword_api(key, query, user_id=user_id, key_lookup=key_lookup,
                                                 outcomes=outcomes, progress=progress))
            if jobspy_sites and search_num < total_searches:
                delay = search_delay_sec + random.uniform(0, 3)
                progress(f"pausing {delay:.0f}s before next search")
                time.sleep(delay)

    for key in feed_keys:
        all_jobs.extend(_run_feed(key, keywords, country=country_indeed, max_age_hours=max_age_hours,
                                  limit=results_per_keyword * max(1, len(keywords)),
                                  outcomes=outcomes, progress=progress, key_lookup=key_lookup))

    if "naukri" in sources and manual_naukri_jobs:
        all_jobs.extend(manual_naukri_jobs)

    if "email_alerts" in sources and inbox_jobs:
        # P5: jobs from the user's job-alert emails, matched per keyword like the feeds. They keep their real
        # source (Naukri, LinkedIn...) so dedupe ranks and the apply buttons work as usual.
        from app.providers.job_sources.base import matches_keyword
        picked = [j for j in inbox_jobs if any(matches_keyword(j, kw) for kw in keywords)]
        progress(f"Job alert emails: {len(picked)} matching jobs (of {len(inbox_jobs)} from your inbox)")
        all_jobs.extend(picked[: results_per_keyword * max(1, len(keywords))])
        outcomes.success("email_alerts")

    if enable_naukri_apify:
        from app.services.naukri_apify_source import fetch_naukri_all_keywords
        apify_jobs = fetch_naukri_all_keywords(
            keywords, locations[0],
            apify_api_token=apify_api_token,
            apify_naukri_actor_id=apify_naukri_actor_id,
            results_per_keyword=results_per_keyword,
            progress=progress,
        )
        if apify_jobs:
            outcomes.success("naukri_apify")
        all_jobs.extend(apify_jobs)

    outcomes.report(on_source_result)
    progress(f"total raw jobs collected: {len(all_jobs)}")
    all_jobs = _validate_jobs(all_jobs, progress)
    all_jobs = _deduplicate_jobs(all_jobs, progress)
    progress(f"final usable jobs: {len(all_jobs)}")
    return all_jobs
