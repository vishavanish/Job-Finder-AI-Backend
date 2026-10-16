"""
app/providers/job_sources/registry.py
----------------------------------------
Every job source the search code can run, keyed like its
`provider_catalog` row (migrations 0004, 0006, 0007). The catalog holds
what an admin can change (on/off, tier, ToS risk, health); this file holds
what code decides (how to call it).

Kinds:
  jobspy   scraped through python-jobspy, one call for all JobSpy sites
  keyword  keyword-search API needing a free key (aggregators.py); with
           several `key_scopes` the first one that has a key and returns
           jobs wins (Google Jobs: Bright Data -> SerpApi -> JSearch)
  feed     whole job feed, cached and filtered per keyword (feeds.py)
  manual   jobs the user pasted in (Naukri)
  apify    paid Apify actor (naukri_apify; driven by enable_naukri_apify)

`user_key_only`: never run on a shared platform/.env key in production —
each user spends their own free quota (owner decision 2026-10-16).
`platform_key_scope`: optional key a feed uses for its shared download.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str        # the `source` value its jobs carry
    kind: str
    jobspy_site: str | None = None
    key_scopes: tuple[str, ...] = ()
    user_key_only: bool = False
    platform_key_scope: str | None = None

    @property
    def key_scope(self) -> str | None:
        return self.key_scopes[0] if self.key_scopes else None


SOURCES: dict[str, SourceSpec] = {s.key: s for s in [
    SourceSpec("linkedin", "LinkedIn", "jobspy", jobspy_site="linkedin"),
    SourceSpec("indeed", "Indeed", "jobspy", jobspy_site="indeed"),
    SourceSpec("glassdoor", "Glassdoor", "jobspy", jobspy_site="glassdoor"),
    SourceSpec("google_jobs", "Google Jobs", "jobspy", jobspy_site="google"),
    SourceSpec("adzuna", "Adzuna", "keyword", key_scopes=("adzuna",)),
    SourceSpec("jooble", "Jooble", "keyword", key_scopes=("jooble",)),
    SourceSpec("careerjet", "Careerjet", "keyword", key_scopes=("careerjet",)),
    SourceSpec("google_jobs_api", "Google Jobs", "keyword", key_scopes=("brightdata", "serpapi", "jsearch"),
               user_key_only=True),
    SourceSpec("remoteok", "RemoteOK", "feed"),
    SourceSpec("remotive", "Remotive", "feed"),
    SourceSpec("himalayas", "Himalayas", "feed"),
    SourceSpec("jobicy", "Jobicy", "feed"),
    SourceSpec("arbeitnow", "Arbeitnow", "feed"),
    SourceSpec("themuse", "The Muse", "feed", platform_key_scope="themuse"),
    SourceSpec("wwr", "We Work Remotely", "feed"),
    SourceSpec("hn_hiring", "HN Who is hiring", "feed"),
    SourceSpec("naukri", "Naukri", "manual"),
    SourceSpec("naukri_apify", "Naukri", "apify", key_scopes=("apify",)),
]}

# Keys a SearchRequest may list in `sources` (naukri_apify is a flag instead).
SEARCHABLE_KEYS = [k for k, s in SOURCES.items() if s.kind != "apify"]

_BY_SITE = {s.jobspy_site: s for s in SOURCES.values() if s.jobspy_site}
_JOBSPY_BY_NAME = {s.name.lower(): s.key for s in SOURCES.values() if s.kind == "jobspy"}


def get(key: str) -> SourceSpec | None:
    return SOURCES.get(key)


def kind(key: str) -> str | None:
    spec = SOURCES.get(key)
    return spec.kind if spec else None


def by_jobspy_site(site: str) -> SourceSpec | None:
    return _BY_SITE.get((site or "").lower())


def key_for_name(name: str) -> str | None:
    """Catalog key of the JobSpy source behind a job's `source` value ("LinkedIn" -> "linkedin")."""
    return _JOBSPY_BY_NAME.get((name or "").strip().lower())
