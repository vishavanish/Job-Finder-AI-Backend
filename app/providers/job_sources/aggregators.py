"""
app/providers/job_sources/aggregators.py
-------------------------------------------
Keyword-search APIs that need a free key (tier `free_key`). The key is
resolved in the worker (user's own key first, then the platform key, then
.env — key_store.resolve) and passed in here; it is never logged or put
in an error message.

All three return a snippet rather than the full description
(source_info.PARTIAL_DESCRIPTION_SOURCES), and their URLs are tracking
redirects to the original posting — fine for Manual apply.

  Adzuna     GET  api.adzuna.com/v1/api/jobs/{country}/search/1   key = "APP_ID:APP_KEY"
  Jooble     POST jooble.org/api/{key}
  Careerjet  GET  search.api.careerjet.net/v4/query               HTTP Basic, key as user name
"""
from __future__ import annotations

import math

from app.providers.job_sources.base import (
    SourceError, SourceQuery, city_part, get_json, is_recent, iso, location_is_remote, make_job, post_json,
)

# Adzuna's country endpoints (ISO 3166-1 alpha-2, lower case).
ADZUNA_COUNTRIES = {
    "india": "in", "united states": "us", "usa": "us", "us": "us", "united kingdom": "gb", "uk": "gb",
    "australia": "au", "austria": "at", "belgium": "be", "brazil": "br", "canada": "ca", "switzerland": "ch",
    "germany": "de", "spain": "es", "france": "fr", "italy": "it", "mexico": "mx", "netherlands": "nl",
    "new zealand": "nz", "poland": "pl", "singapore": "sg", "south africa": "za",
}
CAREERJET_LOCALES = {
    "india": "en_IN", "united states": "en_US", "usa": "en_US", "us": "en_US", "united kingdom": "en_GB",
    "uk": "en_GB", "australia": "en_AU", "canada": "en_CA", "singapore": "en_SG", "ireland": "en_IE",
    "new zealand": "en_NZ", "south africa": "en_ZA", "germany": "de_DE", "france": "fr_FR",
}


def _what_where(q: SourceQuery) -> tuple[str, str]:
    if location_is_remote(q.location):
        return f"{q.keyword} remote", ""
    return q.keyword, city_part(q.location)


def search_adzuna(q: SourceQuery, api_key: str) -> list[dict]:
    country = ADZUNA_COUNTRIES.get(q.country.strip().lower())
    if not country:
        raise SourceError(f"Adzuna: country '{q.country}' is not covered")
    app_id, _, app_key = api_key.partition(":")
    if not (app_id and app_key):
        raise SourceError("Adzuna: key must be saved as APP_ID:APP_KEY")
    what, where = _what_where(q)
    params = {"app_id": app_id, "app_key": app_key, "what": what, "results_per_page": min(q.results_wanted, 50),
              "max_days_old": max(1, math.ceil(q.max_age_hours / 24)), "sort_by": "date"}
    if where:
        params["where"] = where
    data = get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", source="Adzuna", params=params)
    return [make_job(
        source="Adzuna", title=item.get("title"), company=(item.get("company") or {}).get("display_name"),
        location=(item.get("location") or {}).get("display_name"), url=item.get("redirect_url"),
        description=item.get("description"), posted=iso(item.get("created")),
    ) for item in data.get("results") or []]


def search_jooble(q: SourceQuery, api_key: str) -> list[dict]:
    what, where = _what_where(q)
    body = {"keywords": what, "location": where or ("" if location_is_remote(q.location) else q.country),
            "page": "1", "ResultOnPage": str(min(q.results_wanted, 50))}
    # The key is part of the path; get_json's errors never include the URL.
    data = post_json(f"https://jooble.org/api/{api_key}", source="Jooble", json=body)
    jobs = [make_job(
        source="Jooble", title=item.get("title"), company=item.get("company"), location=item.get("location"),
        url=item.get("link"), description=item.get("snippet"), posted=iso(item.get("updated")),
    ) for item in data.get("jobs") or []]
    return [j for j in jobs if is_recent(j["posted"], q.max_age_hours)]


def search_careerjet(q: SourceQuery, api_key: str) -> list[dict]:
    locale = CAREERJET_LOCALES.get(q.country.strip().lower())
    if not locale:
        raise SourceError(f"Careerjet: country '{q.country}' is not covered")
    what, where = _what_where(q)
    params = {"locale_code": locale, "keywords": what, "page_size": min(q.results_wanted, 100), "sort": "date",
              # Careerjet requires the end user's IP and browser; fall back to neutral values when unknown.
              "user_ip": q.client_ip or "127.0.0.1", "user_agent": q.client_user_agent or "JobFinderAI"}
    if where:
        params["location"] = where
    data = get_json("https://search.api.careerjet.net/v4/query", source="Careerjet", params=params,
                    auth=(api_key, ""))
    if data.get("type") not in (None, "JOBS"):
        return []  # e.g. "LOCATIONS": ambiguous location, nothing to show
    jobs = [make_job(
        source="Careerjet", title=item.get("title"), company=item.get("company"),
        location=item.get("locations"), url=item.get("url"), description=item.get("description"),
        posted=iso(item.get("date")),
    ) for item in data.get("jobs") or []]
    return [j for j in jobs if is_recent(j["posted"], q.max_age_hours)]


KEYWORD_APIS = {
    "adzuna": search_adzuna,
    "jooble": search_jooble,
    "careerjet": search_careerjet,
}


# ---------------------------------------------------------------- Google Jobs via the user's own key (P3.2)
# One catalog source ("google_jobs_api") with three providers, tried in the
# order of registry key_scopes; job_sources moves on when one fails or finds
# nothing. All three return Google Jobs listings, so jobs carry source
# "Google Jobs" (rank: job board).

def _country_code(country: str) -> str:
    return ADZUNA_COUNTRIES.get((country or "").strip().lower(), "")


def _google_query(q: SourceQuery) -> str:
    what, where = _what_where(q)
    return f"{what} {where}".strip() if where else f"{what} {q.country}".strip()


def search_serpapi(q: SourceQuery, api_key: str) -> list[dict]:
    params = {"engine": "google_jobs", "q": _google_query(q), "hl": "en", "api_key": api_key}
    if _country_code(q.country):
        params["gl"] = _country_code(q.country)
    data = get_json("https://serpapi.com/search.json", source="SerpApi", params=params)
    if data.get("error") and not data.get("jobs_results"):
        if "hasn't returned any results" in str(data["error"]):
            return []
        raise SourceError(f"SerpApi: {str(data['error'])[:120]}")
    jobs = []
    for item in data.get("jobs_results") or []:
        options = item.get("apply_options") or []
        url = (options[0].get("link") if options else None) or item.get("share_link")
        posted = (item.get("detected_extensions") or {}).get("posted_at", "")
        jobs.append(make_job(source="Google Jobs", title=item.get("title"), company=item.get("company_name"),
                             location=item.get("location"), url=url, description=item.get("description"),
                             posted=iso(posted)))
    return [j for j in jobs if is_recent(j["posted"], q.max_age_hours)]


_JSEARCH_DATE = ((24, "today"), (72, "3days"), (168, "week"), (720, "month"))


def search_jsearch(q: SourceQuery, api_key: str) -> list[dict]:
    date_posted = next((v for hours, v in _JSEARCH_DATE if q.max_age_hours <= hours), "all")
    params = {"query": _google_query(q), "page": 1, "num_pages": 1, "date_posted": date_posted}
    if _country_code(q.country):
        params["country"] = _country_code(q.country)
    data = get_json("https://jsearch.p.rapidapi.com/search", source="JSearch", params=params,
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    jobs = []
    for item in data.get("data") or []:
        location = ", ".join(filter(None, [item.get("job_city"), item.get("job_state"), item.get("job_country")]))
        if item.get("job_is_remote"):
            location = f"{location} (Remote)" if location else "Remote"
        jobs.append(make_job(source="Google Jobs", title=item.get("job_title"), company=item.get("employer_name"),
                             location=location, url=item.get("job_apply_link") or item.get("job_google_link"),
                             description=item.get("job_description"),
                             posted=iso(item.get("job_posted_at_datetime_utc") or item.get("job_posted_at"))))
    return jobs


def _first(item: dict, *names: str):
    for name in names:
        value = item.get(name)
        if value:
            return value
    return ""


def search_brightdata(q: SourceQuery, api_key: str) -> list[dict]:
    """Bright Data SERP API on a Google Jobs URL with parsed JSON.
    EXPERIMENTAL: Bright Data doesn't publish the parsed Google Jobs schema,
    so fields are read defensively and an unrecognised reply yields [] (the
    next provider is then tried). Verify with a real key before relying on it."""
    from urllib.parse import quote_plus

    token, _, zone = api_key.partition(":")
    if not (token and zone):
        raise SourceError("Bright Data: key must be saved as API_KEY:ZONE")
    gl = _country_code(q.country)
    url = (f"https://www.google.com/search?q={quote_plus(_google_query(q) + ' jobs')}&ibp=htl;jobs&hl=en"
           f"{'&gl=' + gl if gl else ''}&brd_json=1")
    data = post_json("https://api.brightdata.com/request", source="Bright Data",
                     json={"zone": zone, "url": url, "format": "raw"},
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    items = []
    if isinstance(data, dict):
        for key in ("jobs", "jobs_results", "job_listings", "google_jobs"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        jobs.append(make_job(
            source="Google Jobs", title=_first(item, "title", "job_title"),
            company=_first(item, "company", "company_name", "employer"),
            location=_first(item, "location", "job_location"),
            url=_first(item, "link", "url", "apply_link", "job_link"),
            description=_first(item, "description", "snippet"),
            posted=iso(_first(item, "posted", "posted_at", "date")),
        ))
    return [j for j in jobs if j["title"] and j["url"].startswith("http") and is_recent(j["posted"], q.max_age_hours)]


KEYWORD_APIS.update({
    "brightdata": search_brightdata,
    "serpapi": search_serpapi,
    "jsearch": search_jsearch,
})
