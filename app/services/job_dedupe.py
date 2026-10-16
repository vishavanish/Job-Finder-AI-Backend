"""
app/services/job_dedupe.py
-----------------------------
One definition of "the same job" for the whole app (CLAUDE.md 7.5):

  key = normalized company | title | city

Case, punctuation and legal suffixes (Pvt, Ltd, Inc, ...) are ignored,
and only the city part of the location is compared, so
"Acme Pvt. Ltd." / "Mumbai, Maharashtra, India" on LinkedIn and
"ACME" / "Mumbai" on Indeed collapse to one key.

When two copies collide, the better-ranked source wins: LinkedIn (it
supports Easy Apply) > company ATS page > job board > aggregator/feed
(app/services/source_info.py); within a rank, the longer description.
"""
from __future__ import annotations

import re

from app.services.source_info import source_rank

_LEGAL_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp",
    "corp", "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "bv",
}
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> list[str]:
    return [w for w in _NON_WORD.split((text or "").lower()) if w]


def normalize_company(company: str) -> str:
    words = _words(company)
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_title(title: str) -> str:
    return " ".join(_words(title))


def normalize_city(location: str) -> str:
    return " ".join(_words((location or "").split(",")[0]))


def job_key(job: dict) -> str:
    company = normalize_company(job.get("company", ""))
    title = normalize_title(job.get("title", ""))
    if not company and not title:
        # Too little to compare on — fall back to the URL so distinct
        # untitled records aren't all merged into one.
        return "url:" + (job.get("url") or "").strip().lower()
    return f"{company}|{title}|{normalize_city(job.get('location', ''))}"


def preferred(existing: dict, candidate: dict) -> dict:
    """Which of two copies of the same job to keep: the better-ranked
    source (source_info.source_rank), then the longer description."""
    existing_rank, candidate_rank = source_rank(existing.get("source")), source_rank(candidate.get("source"))
    if existing_rank != candidate_rank:
        return existing if existing_rank < candidate_rank else candidate
    if len(candidate.get("description") or "") > len(existing.get("description") or ""):
        return candidate
    return existing


def dedupe(jobs: list[dict]) -> list[dict]:
    """Unique jobs in first-seen order, each slot holding the preferred copy."""
    kept: dict[str, dict] = {}
    for job in jobs:
        key = job_key(job)
        kept[key] = preferred(kept[key], job) if key in kept else job
    return list(kept.values())
