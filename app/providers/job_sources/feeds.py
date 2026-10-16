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

import re
import xml.etree.ElementTree as ET  # expat >= 2.4 blocks entity-expansion bombs; no external entities

from app.providers.job_sources.base import SourceError, get_json, get_text, iso, make_job, strip_html

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


def fetch_themuse(api_key: str = "") -> list[dict]:
    """`api_key`: the platform's free registered key, when saved (scope themuse)."""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        params = {"page": page, "location": "Flexible / Remote", **({"api_key": api_key} if api_key else {})}
        data = get_json("https://www.themuse.com/api/public/jobs", source="The Muse", params=params)
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


# ---------------------------------------------------------------- HN "Who is hiring?" (P3.2)
# One thread a month by the "whoishiring" account. Most top-level comments
# start with a header line "Company | Role | Location | ..." (233 of 266 in
# October 2026); posts without one are skipped rather than guessed.

_HN_SPLIT = re.compile(r"\s*\|\s*|\s+[\u2013\u2014-]\s+")
_HN_ROLE = re.compile(r"\b(engineers?|engineering|developers?|scientists?|designers?|managers?|lead|sre|devops|"
                      r"analysts?|architects?|roles?|positions?|product|founding|interns?|head of|cto|staff|"
                      r"recruiters?|marketing|sales|operations|researchers?|swe|programmers?)\b", re.IGNORECASE)
_HN_WHERE = re.compile(r"\b(remote|onsite|on-site|hybrid|in[- ]office)\b", re.IGNORECASE)
_HN_NOT_PLACE = re.compile(r"(full[- ]time|part[- ]time|contract|visa|equity|salary|series [a-e]\b|seed|"
                           r"[$\u20ac\u00a3\u20b9]|\d+k\b|https?://)", re.IGNORECASE)


def _hn_parse(comment: dict) -> dict | None:
    text = comment.get("text") or ""
    header = strip_html(text.split("<p>", 1)[0])
    if not header or len(header) > 400:
        return None
    parts = [p.strip() for p in _HN_SPLIT.split(header) if p and p.strip()]
    if len(parts) < 2:
        return None
    company_part = parts[0]
    company = re.sub(r"\(.*?\)|https?://\S+", "", company_part).strip(" -,")
    rest = parts[1:]
    title = next((p for p in rest if _HN_ROLE.search(p) and not _HN_NOT_PLACE.search(p)), "")
    if not company or not title:
        return None
    places = [p for p in rest if p != title and (_HN_WHERE.search(p) or
              ("," in p and len(p) < 80 and not _HN_NOT_PLACE.search(p) and not _HN_ROLE.search(p)))]
    hint = re.findall(r"\(([^)]*)\)", company_part)
    places += [h for h in hint if _HN_WHERE.search(h)]
    location = "; ".join(places)
    remote = "remote" in header.lower()
    # Remote with a region ("REMOTE (US)") or an on-site city restricts who can apply.
    geo = location if remote else (f"{location} only" if location else "unknown only")
    return {
        **make_job(source="HN Who is hiring", title=title[:200], company=company[:200],
                   location=location or ("Remote" if remote else ""),
                   url=f"https://news.ycombinator.com/item?id={comment.get('id')}",
                   description=text, posted=iso(comment.get("created_at"))),
        "_geo": geo, "_tags": "",
    }


def fetch_hn_hiring() -> list[dict]:
    found = get_json("https://hn.algolia.com/api/v1/search_by_date", source="HN Who is hiring",
                     params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5})
    thread = next((h for h in found.get("hits") or []
                   if str(h.get("title", "")).lower().startswith("ask hn: who is hiring")), None)
    if thread is None or not str(thread.get("objectID", "")).isdigit():
        raise SourceError("HN Who is hiring: this month's thread was not found")
    item = get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}", source="HN Who is hiring")
    jobs = [job for job in (_hn_parse(c) for c in item.get("children") or [] if c.get("text")) if job]
    return jobs


FEEDS["hn_hiring"] = fetch_hn_hiring
