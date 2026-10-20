"""
app/providers/job_sources/base.py
------------------------------------
Shared pieces for job-source adapters (CLAUDE.md 7.4).

Every adapter returns jobs in the plain-dict shape the rest of the app
already uses: source, title, company, location, url, description, posted.
Adapters raise SourceError when the source itself failed (HTTP error,
unexpected payload) so the caller can record source health; "no jobs
found" is an empty list, not an error.

HTTP goes through `request()` so tests can replace it in one place.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

from app.core.config import get_settings

USER_AGENT = "Mozilla/5.0 (compatible; JobFinderAI/1.0; +https://github.com/vishavanish/Job-Finder-AI-Backend)"
MAX_DESCRIPTION_CHARS = 8000

NoOpProgress: Callable[[str], None] = lambda msg: None


class SourceError(Exception):
    """The source failed (not "no results")."""


@dataclass
class SourceQuery:
    keyword: str
    location: str
    country: str = "India"            # SearchRequest.country_indeed
    max_age_hours: int = 24
    results_wanted: int = 25
    client_ip: str = ""                # Careerjet wants the end user's IP + UA
    client_user_agent: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def request(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", get_settings().SOURCE_HTTP_TIMEOUT_SECONDS)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(kwargs.pop("headers", None) or {})}
    return requests.request(method, url, headers=headers, **kwargs)


def _call(method: str, url: str, source: str, **kwargs) -> requests.Response:
    try:
        resp = request(method, url, **kwargs)
    except requests.RequestException as e:
        # The exception text can include the full URL (and a query-string key), so only the type is kept.
        raise SourceError(f"{source}: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise SourceError(f"{source}: HTTP {resp.status_code}")
    return resp


def get_json(url: str, *, source: str, **kwargs) -> Any:
    return _json(_call("GET", url, source, **kwargs), source)


def post_json(url: str, *, source: str, **kwargs) -> Any:
    return _json(_call("POST", url, source, **kwargs), source)


def get_text(url: str, *, source: str, **kwargs) -> str:
    return _call("GET", url, source, **kwargs).text


def _json(resp: requests.Response, source: str) -> Any:
    try:
        return resp.json()
    except ValueError as e:
        raise SourceError(f"{source}: response was not JSON") from e


# ---------------------------------------------------------------- text helpers

_TAG = re.compile(r"<[^>]*(?:>|$)")  # also a tag cut off at the end of a truncated snippet
_SPACE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    plain = html.unescape(_TAG.sub(" ", text or ""))
    return _SPACE.sub(" ", plain).strip()


def clean_description(text: str | None) -> str:
    return strip_html(text)[:MAX_DESCRIPTION_CHARS]


def make_job(*, source: str, title: Any, company: Any, location: Any, url: Any, description: Any = "",
             posted: Any = "") -> dict:
    return {
        "source": source,
        "title": strip_html(str(title or "")),
        "company": strip_html(str(company or "")),
        "location": strip_html(str(location or "")),
        "url": str(url or "").strip(),
        "description": clean_description(str(description or "")),
        "posted": str(posted or ""),
    }


# ---------------------------------------------------------------- dates

_RELATIVE = re.compile(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month)s?\s+ago", re.IGNORECASE)
_RELATIVE_UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800,
                   "month": 2592000}


def parse_date(value: Any) -> datetime | None:
    """Epoch seconds/ms, ISO 8601, RFC 2822 or "3 days ago" -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 10_000_000_000:  # milliseconds
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    text = str(value).strip()
    rel = _RELATIVE.search(text)
    if rel:
        return datetime.now(timezone.utc) - timedelta(seconds=int(rel.group(1)) * _RELATIVE_UNITS[rel.group(2).lower()])
    if text.lower() in ("just posted", "today", "just now"):
        return datetime.now(timezone.utc)
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00").replace(" UTC", "+00:00")),
                  parsedate_to_datetime):
        try:
            dt = parse(text)
        except (ValueError, TypeError, IndexError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def iso(value: Any) -> str:
    dt = parse_date(value)
    return dt.isoformat() if dt else str(value or "")


def is_recent(posted: str, max_age_hours: int) -> bool:
    """Unknown dates count as recent (better to show than to drop)."""
    dt = parse_date(posted)
    if dt is None:
        return True
    return datetime.now(timezone.utc) - dt <= timedelta(hours=max_age_hours)


# ---------------------------------------------------------------- matching

_WORD = re.compile(r"[a-z0-9+#.]+")
# Words that say little about the role on their own.
_GENERIC = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "head", "and", "or", "the", "of",
            "for", "in", "with", "a", "an", "remote", "i", "ii", "iii"}


def _terms(text: str) -> set[str]:
    return {w.strip(".") for w in _WORD.findall((text or "").lower())} - {""}


# Role nouns: shared by most titles, so they never count as a match on their own.
_ROLE_WORDS = {"engineer", "engineering", "developer", "development", "manager", "management", "designer",
               "analyst", "specialist", "consultant", "architect", "executive", "programmer", "scientist",
               "associate", "officer", "administrator", "intern", "director", "coordinator", "expert"}


def matches_keyword(job: dict, keyword: str, extra_text: str = "") -> bool:
    """Loose keyword match for whole-feed sources. The specific words of
    the keyword ("python", "backend", "react native") decide: at least half
    of them in the title or tags, or all of them anywhere in the job. Role
    words ("engineer", "developer") only decide when the keyword has
    nothing else, and then must be in the title. One specific word found
    only in the description is too weak ("python" is mentioned in many
    unrelated posts). Scoring decides real fit."""
    wanted = _terms(keyword) - _GENERIC
    specific = wanted - _ROLE_WORDS
    title = _terms(job.get("title", ""))
    if not specific:
        return not wanted or bool(wanted & title)
    title_and_tags = title | _terms(extra_text)
    if len(specific & title_and_tags) * 2 >= len(specific):
        return True
    return len(specific) >= 2 and specific <= _terms(
        f"{job.get('title', '')} {extra_text} {job.get('description', '')}")


_WORLDWIDE = ("anywhere", "worldwide", "global", "world")
# Words that say how, not where: "REMOTE (US)" is restricted to the US, plain "Remote" is not.
_WORK_MODE_WORDS = re.compile(
    r"\b(remote[- ]first|remote|onsite|on-site|hybrid|fully|full[- ]time|part[- ]time|contract|ok)\b")


def geo_allows(restriction: str, country: str) -> bool:
    """For remote jobs: True when the job's region restriction is empty,
    worldwide, or names the user's country (whole words: "in" must not
    match "United Kingdom")."""
    text = (restriction or "").strip().lower()
    if not re.sub(r"[^a-z]+", "", _WORK_MODE_WORDS.sub(" ", text)):
        return True  # nothing but "Remote" / "Full-time": no region restriction
    if any(w in text for w in _WORLDWIDE) and "only" not in text:
        return True
    c = (country or "").strip().lower()
    aliases = {c} | ({"india", "apac", "asia"} if c == "india" else set())
    return any(a and re.search(rf"\b{re.escape(a)}\b", text) for a in aliases)


def location_is_remote(location: str) -> bool:
    return "remote" in (location or "").lower()


def city_part(location: str) -> str:
    """"Mumbai, Maharashtra, India" -> "Mumbai"; "Remote"/"India" -> ""."""
    first = (location or "").split(",")[0].strip()
    if not first or location_is_remote(first) or first.lower() in {"india", "anywhere"}:
        return ""
    return first
