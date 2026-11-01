"""
app/services/source_info.py
------------------------------
Facts about job sources by their display name (the `source` field every
scraper puts on a job), shared by dedupe, the job store and the scorer.

Rank decides which copy of a duplicated job is kept (owner decision
2026-10-16): LinkedIn (Easy Apply) > the company's own ATS page > job
boards > aggregators and remote feeds. Lower is better; the longer
description only breaks ties within the same rank.
"""
from __future__ import annotations

RANK_LINKEDIN = 0
RANK_COMPANY_ATS = 1
RANK_BOARD = 2
RANK_AGGREGATOR = 3

COMPANY_ATS_SOURCES = {
    "greenhouse", "lever", "smartrecruiters", "workday", "oracle fusion", "successfactors",
    "ashby", "recruitee", "workable", "personio", "breezy", "teamtailor", "rippling", "gem", "keka", "zoho recruit",
    "oracle cloud",
}
AGGREGATOR_SOURCES = {
    "adzuna", "jooble", "careerjet", "findwork",
    "remoteok", "remotive", "himalayas", "jobicy", "arbeitnow", "the muse", "we work remotely", "working nomads",
}
# Sources whose API returns a snippet, not the full job description.
PARTIAL_DESCRIPTION_SOURCES = {"adzuna", "jooble", "careerjet"}


def _name(source: str | None) -> str:
    return (source or "").strip().lower()


def source_rank(source: str | None) -> int:
    name = _name(source)
    if name == "linkedin":
        return RANK_LINKEDIN
    if name in COMPANY_ATS_SOURCES:
        return RANK_COMPANY_ATS
    if name in AGGREGATOR_SOURCES:
        return RANK_AGGREGATOR
    return RANK_BOARD  # Indeed, Glassdoor, Naukri, Google Jobs and anything unknown


def has_partial_description(source: str | None) -> bool:
    return _name(source) in PARTIAL_DESCRIPTION_SOURCES
