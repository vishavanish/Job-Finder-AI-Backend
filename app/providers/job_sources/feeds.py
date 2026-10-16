"""
app/providers/job_sources/feeds.py
-------------------------------------
Free, keyless remote-job feeds. Each fetcher downloads the whole (recent)
feed; job_sources caches it in Redis and filters it per keyword, so a run
with ten keywords still makes one request per feed per TTL.

Each job carries two private fields used only for filtering, removed
before the job leaves job_sources: `_geo` (who may apply: "Worldwide",
"USA only", "India", ...) and `_tags`.

Terms (verified 2026-10-16): RemoteOK asks for a link back to the job
page; Remotive forbids reselling its listings; Arbeitnow asks for a link
back. We only show each job with its source and original URL.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET  # expat >= 2.4 blocks entity-expansion bombs; no external entities

from app.providers.job_sources.base import SourceError, get_json, get_text, iso, make_job

MAX_PAGES = 3


def _with_meta(job: dict, geo: str = "", tags=None) -> dict:
    job["_geo"] = geo or ""
    job["_tags"] = " ".join(str(t) for t in (tags or []))
    return job


def fetch_remoteok() -> list[dict]:
    data = get_json("https://remoteok.com/api", source="RemoteOK")
    if not isinstance(data, list):
        raise SourceError("RemoteOK: unexpected payload")
    jobs = []
    for item in data:
        if not isinstance(item, dict) or not item.get("position"):
            continue  # the first element is the legal notice
        jobs.append(_with_meta(make_job(
            source="RemoteOK", title=item.get("position"), company=item.get("company"),
            location=item.get("location") or "Remote", url=item.get("url") or item.get("apply_url"),
            description=item.get("description"), posted=iso(item.get("date") or item.get("epoch")),
        ), geo=item.get("location", ""), tags=item.get("tags")))
    return jobs


def fetch_remotive() -> list[dict]:
    data = get_json("https://remotive.com/api/remote-jobs", source="Remotive")
    jobs = []
    for item in data.get("jobs") or []:
        geo = item.get("candidate_required_location") or ""
        jobs.append(_with_meta(make_job(
            source="Remotive", title=item.get("title"), company=item.get("company_name"),
            location=f"Remote ({geo})" if geo else "Remote", url=item.get("url"),
            description=item.get("description"), posted=iso(item.get("publication_date")),
        ), geo=geo, tags=(item.get("tags") or []) + [item.get("category") or ""]))
    return jobs


def fetch_himalayas() -> list[dict]:
    jobs, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"limit": 20, **({"cursor": cursor} if cursor else {})}
        data = get_json("https://himalayas.app/jobs/api", source="Himalayas", params=params)
        for item in data.get("jobs") or []:
            geo = ", ".join(item.get("locationRestrictions") or [])
            jobs.append(_with_meta(make_job(
                source="Himalayas", title=item.get("title"), company=item.get("companyName"),
                location=f"Remote ({geo})" if geo else "Remote",
                url=item.get("applicationLink") or item.get("guid"),
                description=item.get("description") or item.get("excerpt"), posted=iso(item.get("pubDate")),
            ), geo=geo, tags=item.get("categories")))
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return jobs


def fetch_jobicy() -> list[dict]:
    data = get_json("https://jobicy.com/api/v2/remote-jobs", source="Jobicy", params={"count": 50})
    jobs = []
    for item in data.get("jobs") or []:
        geo = item.get("jobGeo") or ""
        jobs.append(_with_meta(make_job(
            source="Jobicy", title=item.get("jobTitle"), company=item.get("companyName"),
            location=f"Remote ({geo})" if geo else "Remote", url=item.get("url"),
            description=item.get("jobDescription") or item.get("jobExcerpt"), posted=iso(item.get("pubDate")),
        ), geo=geo, tags=item.get("jobIndustry")))
    return jobs


def fetch_arbeitnow() -> list[dict]:
    data = get_json("https://www.arbeitnow.com/api/job-board-api", source="Arbeitnow")
    jobs = []
    for item in data.get("data") or []:
        if not item.get("remote"):
            continue  # on-site jobs in Germany are out of scope for a remote feed
        # Remote roles here are almost always Germany/EU-based, so the city is the restriction.
        geo = item.get("location") or "Germany"
        jobs.append(_with_meta(make_job(
            source="Arbeitnow", title=item.get("title"), company=item.get("company_name"),
            location=f"Remote ({geo})", url=item.get("url"), description=item.get("description"),
            posted=iso(item.get("created_at")),
        ), geo=f"{geo} only", tags=item.get("tags")))
    return jobs


def fetch_themuse() -> list[dict]:
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        data = get_json("https://www.themuse.com/api/public/jobs", source="The Muse",
                        params={"page": page, "location": "Flexible / Remote"})
        for item in data.get("results") or []:
            locations = ", ".join(loc.get("name", "") for loc in item.get("locations") or [])
            remote = any(w in locations.lower() for w in ("remote", "flexible"))
            jobs.append(_with_meta(make_job(
                source="The Muse", title=item.get("name"), company=(item.get("company") or {}).get("name"),
                location=locations or "Remote", url=(item.get("refs") or {}).get("landing_page"),
                description=item.get("contents"), posted=iso(item.get("publication_date")),
            ), geo="" if remote else f"{locations} only",
                tags=[c.get("name", "") for c in item.get("categories") or []]))
        if page >= int(data.get("page_count") or 1):
            break
    return jobs


def fetch_wwr() -> list[dict]:
    text = get_text("https://weworkremotely.com/remote-jobs.rss", source="We Work Remotely",
                    headers={"Accept": "application/rss+xml, application/xml"})
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError("We Work Remotely: feed was not valid XML") from e
    jobs = []
    for item in root.iterfind("channel/item"):
        def tag(name: str) -> str:
            return (item.findtext(name) or "").strip()
        company, _, title = tag("title").partition(": ")
        if not title:
            company, title = "", company
        region = tag("region")
        jobs.append(_with_meta(make_job(
            source="We Work Remotely", title=title, company=company,
            location=f"Remote ({region})" if region else "Remote", url=tag("link"),
            description=tag("description"), posted=iso(tag("pubDate")),
        ), geo=region, tags=[tag("category"), tag("skills")]))
    return jobs


FEEDS = {
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "himalayas": fetch_himalayas,
    "jobicy": fetch_jobicy,
    "arbeitnow": fetch_arbeitnow,
    "themuse": fetch_themuse,
    "wwr": fetch_wwr,
}
