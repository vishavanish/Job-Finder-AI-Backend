"""
app/providers/job_sources/linkedin_applicants.py
---------------------------------------------------
Applicant count for a LinkedIn job ("143 applicants", "Over 200
applicants") from LinkedIn's public job page — the same guest page JobSpy
reads descriptions from (P3.2). Only called for jobs that reach the feed,
paced, and stopped at the first 429. Requests identify as Job Finder AI.
"""
from __future__ import annotations

import re

import requests

from app.providers.job_sources.base import request

_JOB_ID = re.compile(r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d{6,})")
_COUNT = re.compile(r"(over\s+)?([\d,]+)\s+applicants?", re.IGNORECASE)
_FIRST = re.compile(r"be among the first\s+([\d,]+)\s+applicants", re.IGNORECASE)


class RateLimited(Exception):
    pass


def job_id(url: str) -> str | None:
    match = _JOB_ID.search(url or "")
    return match.group(1) if match else None


def parse_count(page: str) -> str | None:
    """"143", "Over 200", "Under 25" or None."""
    first = _FIRST.search(page or "")
    if first:
        return f"Under {first.group(1).replace(',', '')}"
    match = _COUNT.search(page or "")
    if not match:
        return None
    number = match.group(2).replace(",", "")
    return f"Over {number}" if match.group(1) else number


def fetch_count(url: str) -> str | None:
    """None when the page has no count or can't be read; RateLimited on 429."""
    jid = job_id(url)
    if not jid:
        return None
    try:
        resp = request("GET", f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
                       headers={"Accept": "text/html"})
    except requests.RequestException:
        return None
    if resp.status_code == 429:
        raise RateLimited()
    return parse_count(resp.text) if resp.status_code == 200 else None
