"""
app/services/company_ats_sources.py
--------------------------------------
Ported from company_ats_sources.py. COMPANY_ATS_TARGETS is no longer a
hardcoded list — it comes from CompanyAtsRequest.targets in the API call.

CUSTOM PLATFORM STRATEGY (three-tier fallback, in order):
  1. Hostname-based platform detection (e.g. *.myworkdayjobs.com) routes
     straight to a dedicated adapter (fetch_workday) that knows the
     platform's real JSON API shape.
  2. If detection is inconclusive, try a plain JSON probe against
     custom_endpoint_url anyway — some "unknown"/vanity-domain tenants
     still expose a usable JSON endpoint at the given URL.
  3. If neither works (common for JS-rendered boards like SuccessFactors),
     fall back to a headless-Playwright DOM scrape — but only if the
     caller supplied CSS selectors on the target. Otherwise fail loudly
     with a message telling the caller what to inspect, rather than
     silently returning [].
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import urlparse
import logging
import requests


HEADERS = {"User-Agent": "Mozilla/5.0 (job-finder API)"}
logger = logging.getLogger("job_finder_api.ats")
NoOpProgress: Callable[[str], None] = lambda msg: None

_PLATFORM_HINTS = {
    "myworkdayjobs.com": "workday",
    "successfactors.com": "successfactors",
    "career": "successfactors",  # vanity domains (careers.<company>.com) often proxy SF
    "taleo.net": "taleo",
    "icims.com": "icims",
    "fa.oraclecloud.com": "oracle_fusion",
}

# Registry of known Oracle Fusion tenants — each needs a session_url (the
# human-facing career page, primes the anonymous session cookie) alongside
# the JSON API endpoint_url passed in via custom_endpoint_url. Add new
# Oracle Fusion companies here — no other code changes needed.
KNOWN_ORACLE_FUSION_SESSION_URLS = {
    "jpmorgan": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
    # "somecompany": "https://<tenant>.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<SITE>/jobs",
}


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def detect_platform(endpoint_url: str) -> str:
    """Best-effort fingerprint from the hostname AND path — cheap, no
    extra request needed. Checks the hostname against known ATS domains
    first; if that's inconclusive, checks the URL path for SuccessFactors'
    distinctive '/search/' + job-listing route pattern (careers.ey.com/ey/
    search/ etc.), since many SuccessFactors tenants sit on a custom vanity
    domain (careers.<company>.com) that won't match any hostname hint.
    Returns 'unknown' if nothing matches, so the caller knows to try the
    JSON probe first and Playwright second."""
    parsed = urlparse(endpoint_url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    for hint, platform in _PLATFORM_HINTS.items():
        if hint in host:
            return platform

    # SuccessFactors career sites commonly expose a "/<tenant>/search/" or
    # "/<tenant>/job/" route regardless of the vanity domain used.
    if re.search(r"/[a-z0-9_-]+/(search|job)/?", path):
        return "successfactors"

    return "unknown"

def fetch_greenhouse(company_slug: str, progress: Callable[[str], None] = NoOpProgress) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            progress(f"Greenhouse '{company_slug}' returned {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        progress(f"Greenhouse '{company_slug}' fetch failed: {e}")
        return []

    jobs = []
    for item in data.get("jobs", []):
        jobs.append({
            "source": "Greenhouse",
            "title": item.get("title", ""),
            "company": company_slug,
            "location": (item.get("location") or {}).get("name", ""),
            "url": item.get("absolute_url", ""),
            "description": _strip_html(item.get("content", "")),
            "posted": item.get("updated_at", ""),
        })
    progress(f"Greenhouse '{company_slug}': {len(jobs)} jobs")
    return jobs


def fetch_lever(company_slug: str, progress: Callable[[str], None] = NoOpProgress) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            progress(f"Lever '{company_slug}' returned {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        progress(f"Lever '{company_slug}' fetch failed: {e}")
        return []

    jobs = []
    for item in data:
        categories = item.get("categories", {}) or {}
        desc_html = item.get("descriptionPlain") or item.get("description", "")
        jobs.append({
            "source": "Lever",
            "title": item.get("text", ""),
            "company": company_slug,
            "location": categories.get("location", ""),
            "url": item.get("hostedUrl", ""),
            "description": _strip_html(desc_html),
            "posted": str(item.get("createdAt", "")),
        })
    progress(f"Lever '{company_slug}': {len(jobs)} jobs")
    return jobs


def fetch_smartrecruiters(company_slug: str, progress: Callable[[str], None] = NoOpProgress) -> list[dict]:
    url = f"https://api.smartrecruiters.com/v1/companies/{company_slug}/postings"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            progress(f"SmartRecruiters '{company_slug}' returned {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        progress(f"SmartRecruiters '{company_slug}' fetch failed: {e}")
        return []

    jobs = []
    for item in data.get("content", []):
        location = item.get("location", {}) or {}
        loc_str = ", ".join(filter(None, [location.get("city"), location.get("country")]))
        jd = item.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "") if item.get("jobAd") else ""
        jobs.append({
            "source": "SmartRecruiters",
            "title": item.get("name", ""),
            "company": company_slug,
            "location": loc_str,
            "url": item.get("applyUrl") or item.get("ref", ""),
            "description": _strip_html(jd),
            "posted": item.get("releasedDate", ""),
        })
    progress(f"SmartRecruiters '{company_slug}': {len(jobs)} jobs")
    return jobs


def fetch_workday(tenant_url: str, company_slug: str, progress: Callable[[str], None] = NoOpProgress) -> list[dict]:
    """Workday exposes a JSON search API at /wday/cxs/<tenant>/<site>/jobs
    once you know the tenant + site slugs — both of which are embedded in
    the career page URL itself, e.g.:
        https://<tenant>.wdX.myworkdayjobs.com/<site>/...
    """
    parsed = urlparse(tenant_url)
    parts = [p for p in parsed.path.split("/") if p]
    site = parts[0] if parts else ""
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    if not (site and tenant):
        progress(f"Workday '{company_slug}': could not derive tenant/site from URL '{tenant_url}' — skipping")
        return []

    api_url = f"https://{parsed.hostname}/wday/cxs/{tenant}/{site}/jobs"
    try:
        resp = requests.post(api_url, headers=HEADERS, json={"limit": 50, "offset": 0}, timeout=20)
        if resp.status_code != 200:
            progress(f"Workday '{company_slug}' API returned {resp.status_code} for {api_url}")
            return []
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        progress(f"Workday '{company_slug}' fetch failed: {e}")
        return []

    jobs = []
    for item in data.get("jobPostings", []):
        jobs.append({
            "source": company_slug.title(),
            "title": item.get("title", ""),
            "company": company_slug,
            "location": item.get("locationsText", ""),
            "url": f"https://{parsed.hostname}{item.get('externalPath', '')}",
            "description": "",  # Workday requires a second per-job call for full JD; left blank here
            "posted": item.get("postedOn", ""),
        })
    progress(f"Workday '{company_slug}': {len(jobs)} jobs")
    return jobs

def fetch_oracle_fusion(
    company_slug: str,
    endpoint_url: str,
    session_url: str,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    """Oracle Fusion/HCM career sites (jpmc.fa.oraclecloud.com and similar
    <tenant>.fa.oraclecloud.com domains) expose a public JSON API at
    /hcmRestApi/resources/latest/recruitingCEJobRequisitions — but it only
    returns real data once the client holds a session cookie obtained by
    first visiting the human-facing career page. No login/candidate
    account is required; the cookie is anonymous and issued on first
    visit (verified: incognito browsing with no prior visit history still
    gets full job data once this priming step happens).

    Paginates via the response's own {count, items, hasMore, offset,
    limit} shape until hasMore is False.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Accept"] = "application/json, application/vnd.oracle.adf.resourcecollection+json;q=0.9, */*;q=0.8"

    try:
        session.get(session_url, timeout=15)  # primes the session cookie jar
    except Exception as e:  # noqa: BLE001
        progress(f"Oracle Fusion '{company_slug}': failed to prime session from {session_url}: {e}")
        return []

    jobs: list[dict] = []
    url = endpoint_url
    while url:
        try:
            resp = session.get(
                url,
                headers={"Content-Type": "application/vnd.oracle.adf.resourceitem+json;charset=utf-8"},
                timeout=20,
            )
            if resp.status_code != 200:
                progress(f"Oracle Fusion '{company_slug}' returned {resp.status_code}")
                break
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            progress(f"Oracle Fusion '{company_slug}' fetch failed: {e}")
            break

        for item in data.get("items", []):
            jobs.append({
                "source": company_slug.title(),
                "title": item.get("Title", ""),
                "company": company_slug,
                "location": item.get("PrimaryLocation", "") or "",
                "url": f"{urlparse(endpoint_url).scheme}://{urlparse(endpoint_url).hostname}/hcmUI/CandidateExperience/en/sites/CX_1001/job/{item.get('Id', '')}",
                "description": _strip_html(item.get("ExternalDescriptionStr", "")),
                "posted": item.get("PostedDate", ""),
            })

        has_more = bool(data.get("hasMore", False))
        if not has_more:
            break
        offset = data.get("offset", 0)
        limit = data.get("limit", len(data.get("items", [])) or 1)
        next_offset = offset + limit
        if f"offset={offset}" in url:
            url = url.replace(f"offset={offset}", f"offset={next_offset}")
        else:
            url = f"{url}&offset={next_offset}"

    progress(f"Oracle Fusion '{company_slug}': {len(jobs)} jobs")
    return jobs


# app/services/company_ats_sources.py — add this function

def fetch_successfactors(
    base_search_url: str,
    company_slug: str,
    *,
    max_pages: int = 5,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    """SuccessFactors career sites (e.g. careers.ey.com/ey/search/) render
    job listings as plain server-side HTML — no JS rendering needed.
    Paginates via ?startrow=N, 25 results per page."""
    from bs4 import BeautifulSoup

    jobs: list[dict] = []
    parsed_base = urlparse(base_search_url)
    origin = f"{parsed_base.scheme}://{parsed_base.hostname}"

    for page_num in range(max_pages):
        startrow = page_num * 25
        sep = "&" if "?" in base_search_url else "?"
        page_url = f"{base_search_url}{sep}startrow={startrow}" if startrow else base_search_url

        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                progress(f"SuccessFactors '{company_slug}' page {page_num + 1} returned {resp.status_code}")
                break
        except Exception as e:  # noqa: BLE001
            progress(f"SuccessFactors '{company_slug}' fetch failed: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr")  # each job row; refine to a table id/class if the site has one
        page_jobs = []
        for row in rows:
            link = row.find("a", href=True)
            if not link or "/job/" not in link["href"]:
                continue
            href = link["href"]
            if href.startswith("/"):
                href = origin + href
            title = link.get_text(strip=True)
            cells = row.find_all("td")
            location = cells[1].get_text(strip=True) if len(cells) > 1 else ""

            page_jobs.append({
                "source": company_slug.title(),
                "title": title,
                "company": company_slug,
                "location": location,
                "url": href,
                "description": "",  # fetch the job detail page separately if you need full JD
                "posted": "",
            })

        if not page_jobs:
            break
        jobs.extend(page_jobs)
        progress(f"SuccessFactors '{company_slug}' page {page_num + 1}: {len(page_jobs)} jobs")

    # de-dupe by URL — the same job link appears twice per row in EY's markup
    seen, unique = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            unique.append(j)

    progress(f"SuccessFactors '{company_slug}': {len(unique)} total jobs")
    return unique


def fetch_custom_rendered(
    company_slug: str,
    endpoint_url: str,
    *,
    list_selector: str,
    title_selector: str | None,
    link_selector: str | None,
    location_selector: str | None,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    """Headless-Playwright DOM scrape for career pages that render
    listings client-side (no scrapeable JSON API found). Reuses the same
    Chromium engine already used in browser_apply.py — no new dependency,
    still free (no paid API involved)."""
    from playwright.sync_api import sync_playwright

    jobs: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(endpoint_url, timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(2000)  # let any final XHR-driven render settle

            cards = page.query_selector_all(list_selector)
            for card in cards:
                title_el = card.query_selector(title_selector) if title_selector else None
                link_el = card.query_selector(link_selector) if link_selector else card
                loc_el = card.query_selector(location_selector) if location_selector else None

                href = link_el.get_attribute("href") if link_el else ""
                if href and href.startswith("/"):
                    href = f"{urlparse(endpoint_url).scheme}://{urlparse(endpoint_url).hostname}{href}"

                jobs.append({
                    "source": company_slug.title(),
                    "title": (title_el.inner_text().strip() if title_el else ""),
                    "company": company_slug,
                    "location": (loc_el.inner_text().strip() if loc_el else ""),
                    "url": href or "",
                    "description": "",
                    "posted": "",
                })
            browser.close()
    except Exception as e:  # noqa: BLE001
        progress(f"Playwright render fallback for '{company_slug}' failed: {e}")
        return []

    jobs = [j for j in jobs if j["title"] and j["url"]]
    progress(f"Custom (rendered) '{company_slug}': {len(jobs)} jobs")
    return jobs


def fetch_custom(
    company_slug: str,
    endpoint_url: str,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    *,
    list_selector: str | None = None,
    title_selector: str | None = None,
    link_selector: str | None = None,
    location_selector: str | None = None,
    session_url: str | None = None,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    platform = detect_platform(endpoint_url)

    if platform == "workday":
        return fetch_workday(endpoint_url, company_slug, progress=progress)
    if platform == "successfactors":
        return fetch_successfactors(endpoint_url, company_slug, progress=progress)
    if platform == "oracle_fusion":
        resolved_session_url = session_url or KNOWN_ORACLE_FUSION_SESSION_URLS.get(company_slug.lower())
        if not resolved_session_url:
            progress(
                f"Oracle Fusion '{company_slug}': no custom_session_url given and no known "
                f"default for this slug — cannot prime session. Supply custom_session_url "
                f"(the human-facing career page URL) alongside custom_endpoint_url."
            )
            return []
        return fetch_oracle_fusion(company_slug, endpoint_url, resolved_session_url, progress=progress)
    data = None
    try:
        if method.upper() == "POST":
            resp = requests.post(endpoint_url, headers=HEADERS, json=json_body or {}, timeout=20)
        else:
            resp = requests.get(endpoint_url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
    except Exception:
        data = None

    if data:
        raw_postings = data.get("jobPostings", data.get("jobs", []))
        if raw_postings:
            jobs = []
            for item in raw_postings:
                jobs.append({
                    "source": company_slug.title(),
                    "title": item.get("title", ""),
                    "company": company_slug,
                    "location": item.get("locationsText", item.get("location", "")),
                    "url": item.get("externalPath", item.get("url", "")),
                    "description": _strip_html(item.get("jobDescription", "")),
                    "posted": item.get("postedOn", ""),
                })
            progress(f"Custom (JSON) '{company_slug}': {len(jobs)} jobs")
            return jobs

    # JSON probe returned nothing usable — this is the EY/SuccessFactors
    # case. Fall back to Playwright IF the caller gave selectors;
    # otherwise fail loudly instead of silently returning [].
    if list_selector:
        return fetch_custom_rendered(
            company_slug, endpoint_url,
            list_selector=list_selector,
            title_selector=title_selector,
            link_selector=link_selector,
            location_selector=location_selector,
            progress=progress,
        )

    progress(
        f"Custom '{company_slug}': no JSON API found (detected platform: {platform}) and "
        f"no custom_list_selector provided — cannot scrape this page. Inspect the site's "
        f"DOM and pass custom_list_selector/custom_title_selector/custom_link_selector."
    )
    return []


def scrape_company_pages(
    targets: list[dict],
    *,
    request_delay_sec: float = 2,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[dict]:
    """`targets` is a list of dicts matching CompanyAtsTarget:
    {"platform": "greenhouse"|"lever"|"smartrecruiters"|"custom", "slug": "...",
     "custom_endpoint_url": ..., "custom_method": ..., "custom_json_body": ...,
     "custom_list_selector": ..., "custom_title_selector": ...,
     "custom_link_selector": ..., "custom_location_selector": ...}
    """
    all_jobs: list[dict] = []
    for i, target in enumerate(targets):
        platform = (target.get("platform") or "").lower()
        slug = target.get("slug", "")
        if not slug:
            continue

        if platform == "greenhouse":
            all_jobs.extend(fetch_greenhouse(slug, progress))
        elif platform == "lever":
            all_jobs.extend(fetch_lever(slug, progress))
        elif platform == "smartrecruiters":
            all_jobs.extend(fetch_smartrecruiters(slug, progress))
        elif platform == "custom":
            endpoint = target.get("custom_endpoint_url")
            if not endpoint:
                progress(f"'{slug}' is platform=custom but no custom_endpoint_url was given — skipping")
            else:
                all_jobs.extend(fetch_custom(
                    slug, endpoint,
                    method=target.get("custom_method", "GET"),
                    json_body=target.get("custom_json_body"),
                    list_selector=target.get("custom_list_selector"),
                    title_selector=target.get("custom_title_selector"),
                    link_selector=target.get("custom_link_selector"),
                    location_selector=target.get("custom_location_selector"),
                    session_url=target.get("custom_session_url"),
                    progress=progress,
                ))
        else:
            progress(f"Unknown ATS platform '{platform}' for '{slug}' — skipping")

        if i < len(targets) - 1:
            time.sleep(request_delay_sec)

    progress(f"total company-career-page jobs collected: {len(all_jobs)}")
    return all_jobs