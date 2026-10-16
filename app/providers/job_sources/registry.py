"""
app/providers/job_sources/registry.py
----------------------------------------
Every job source the search code can run, keyed like its
`provider_catalog` row (migrations 0004, 0006). The catalog holds what an
admin can change (on/off, tier, ToS risk, health); this file holds what
code decides (how to call it).

Kinds:
  jobspy   scraped through python-jobspy, one call for all JobSpy sites
  keyword  keyword-search API needing a free key (aggregators.py)
  feed     whole remote-job feed, cached and filtered per keyword (feeds.py)
  manual   jobs the user pasted in (Naukri)
  apify    paid Apify actor (naukri_apify; driven by enable_naukri_apify)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str        # the `source` value its jobs carry
    kind: str
    jobspy_site: str | None = None
    key_scope: str | None = None


SOURCES: dict[str, SourceSpec] = {s.key: s for s in [
    SourceSpec("linkedin", "LinkedIn", "jobspy", jobspy_site="linkedin"),
    SourceSpec("indeed", "Indeed", "jobspy", jobspy_site="indeed"),
    SourceSpec("glassdoor", "Glassdoor", "jobspy", jobspy_site="glassdoor"),
    SourceSpec("google_jobs", "Google Jobs", "jobspy", jobspy_site="google"),
    SourceSpec("adzuna", "Adzuna", "keyword", key_scope="adzuna"),
    SourceSpec("jooble", "Jooble", "keyword", key_scope="jooble"),
    SourceSpec("careerjet", "Careerjet", "keyword", key_scope="careerjet"),
    SourceSpec("remoteok", "RemoteOK", "feed"),
    SourceSpec("remotive", "Remotive", "feed"),
    SourceSpec("himalayas", "Himalayas", "feed"),
    SourceSpec("jobicy", "Jobicy", "feed"),
    SourceSpec("arbeitnow", "Arbeitnow", "feed"),
    SourceSpec("themuse", "The Muse", "feed"),
    SourceSpec("wwr", "We Work Remotely", "feed"),
    SourceSpec("naukri", "Naukri", "manual"),
    SourceSpec("naukri_apify", "Naukri", "apify", key_scope="apify"),
]}

# Keys a SearchRequest may list in `sources` (naukri_apify is a flag instead).
SEARCHABLE_KEYS = [k for k, s in SOURCES.items() if s.kind != "apify"]

_BY_SITE = {s.jobspy_site: s for s in SOURCES.values() if s.jobspy_site}
_BY_NAME = {s.name.lower(): s.key for s in SOURCES.values() if s.kind != "apify"}


def get(key: str) -> SourceSpec | None:
    return SOURCES.get(key)


def kind(key: str) -> str | None:
    spec = SOURCES.get(key)
    return spec.kind if spec else None


def by_jobspy_site(site: str) -> SourceSpec | None:
    return _BY_SITE.get((site or "").lower())


def key_for_name(name: str) -> str | None:
    """Catalog key for a job's `source` value ("LinkedIn" -> "linkedin")."""
    return _BY_NAME.get((name or "").strip().lower())
