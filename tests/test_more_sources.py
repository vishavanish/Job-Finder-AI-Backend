"""QA7 decisions 1, 2, 4 (2026-10-31): Working Nomads feed, HN job posts (YC), Findwork.dev keyed API."""
from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.models.ai import ProviderCatalog
from app.providers.job_sources import aggregators, base, feeds, registry
from app.providers.job_sources.base import SourceError, SourceQuery
from app.services import job_sources
from app.services.key_store import ResolvedKey

WORKING_NOMADS = [
    {"url": "https://www.workingnomads.com/jobs/senior-python-developer-acme", "title": "Senior Python Developer",
     "description": "<p>Build APIs with <b>FastAPI</b>.</p>", "company_name": "Acme", "category_name": "Development",
     "tags": "python,fastapi", "location": "Anywhere", "pub_date": "2026-10-30T09:50:51-04:00"},
    {"url": "https://www.workingnomads.com/jobs/magento-dev-lemon", "title": "Senior Magento Developer",
     "description": "PHP", "company_name": "Lemon.io", "category_name": "Development", "tags": "php",
     "location": "Europe, LATAM", "pub_date": "2026-10-29T09:50:51-04:00"},
]
HN_JOBS = {"hits": [
    {"title": "Converge (YC S23) Is Hiring a Founding Platform Engineer",
     "url": "https://job-boards.greenhouse.io/converge/jobs/123", "created_at": "2026-10-28T17:00:24Z"},
    {"title": "Wasmer (YC S19) Is Hiring OS/Hypervisor Experts in Europe",
     "url": "https://www.workatastartup.com/jobs/98722", "created_at": "2026-10-30T07:00:35Z"},
    {"title": "9 Mothers (YC P26) Is Hiring in Austin, TX", "url": "https://9mothers.com/careers",
     "created_at": "2026-10-21T12:00:24Z"},
    {"title": "Remotely (YC W24) is hiring remote backend engineers", "url": "https://remotely.dev/careers",
     "created_at": "2026-10-20T12:00:24Z"},
    {"title": "Show HN: not a job post", "url": "https://example.com", "created_at": "2026-10-20T12:00:24Z"},
    {"title": "Textonly (YC S20) Is Hiring Engineers", "url": None, "created_at": "2026-10-20T12:00:24Z"},
]}
FINDWORK = {"count": 2, "results": [
    {"id": 1, "role": "Backend Engineer (Python)", "company_name": "Beta", "location": "Berlin", "remote": False,
     "url": "https://findwork.dev/1/backend", "text": "Python, Postgres", "date_posted": "2026-10-30T10:00:00Z"},
    {"id": 2, "role": "Data Engineer", "company_name": "Gamma", "location": None, "remote": True,
     "url": "https://findwork.dev/2/data", "text": "Spark", "date_posted": "2026-10-29T10:00:00Z"},
]}


class FakeResponse:
    def __init__(self, body, status_code: int = 200):
        self._body, self.status_code, self.headers = body, status_code, {}
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture
def http(monkeypatch):
    routes: dict[str, object] = {}
    calls: list[dict] = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        for part, body in routes.items():
            if part in url:
                return body if isinstance(body, FakeResponse) else FakeResponse(body)
        return FakeResponse({"error": "not routed"}, 404)

    monkeypatch.setattr(base, "request", fake)
    return routes, calls


def test_working_nomads_jobs_carry_region_and_description(http):
    routes, _ = http
    routes["workingnomads.com"] = WORKING_NOMADS
    jobs = {j["company"]: j for j in feeds.fetch_working_nomads()}
    assert jobs["Acme"]["location"] == "Remote" and jobs["Acme"]["_geo"] == ""
    assert "FastAPI" in jobs["Acme"]["description"] and jobs["Acme"]["posted"].startswith("2026-10-30")
    lemon = jobs["Lemon.io"]
    assert lemon["location"] == "Remote (Europe, LATAM)" and base.geo_allows(lemon["_geo"], "India") is False


def test_hn_job_posts_become_jobs_with_the_company_link(http):
    routes, _ = http
    routes["search_by_date"] = HN_JOBS
    jobs = {j["company"]: j for j in feeds.fetch_hn_job_posts()}
    assert set(jobs) == {"Converge", "Wasmer", "9 Mothers", "Remotely"}
    assert jobs["Converge"]["title"] == "Founding Platform Engineer"
    assert jobs["Converge"]["url"] == "https://job-boards.greenhouse.io/converge/jobs/123"  # harvested later
    assert jobs["Wasmer"]["title"] == "OS/Hypervisor Experts" and jobs["Wasmer"]["location"] == "Europe"
    assert jobs["9 Mothers"]["title"] == "Open roles" and jobs["9 Mothers"]["_geo"] == "Austin, TX only"
    assert jobs["Remotely"]["location"] == "Remote" and jobs["Remotely"]["_geo"] == ""


def test_hn_source_keeps_the_job_posts_when_the_monthly_thread_is_missing(http):
    routes, _ = http
    routes["search_by_date"] = HN_JOBS  # no "Ask HN: Who is hiring" story in it
    jobs = feeds.fetch_hn_hiring()
    assert {j["company"] for j in jobs} >= {"Converge", "Wasmer"}
    routes["search_by_date"] = FakeResponse({}, 500)
    with pytest.raises(SourceError):
        feeds.fetch_hn_hiring()


def test_findwork_sends_the_token_and_reads_results(http):
    routes, calls = http
    routes["findwork.dev"] = FINDWORK
    q = SourceQuery(keyword="Python Developer", location="Berlin, Germany", max_age_hours=24 * 7)
    jobs = aggregators.search_findwork(q, "fw-key")
    assert [j["title"] for j in jobs] == ["Backend Engineer (Python)", "Data Engineer"]
    assert jobs[1]["location"] == "Remote" and jobs[0]["source"] == "Findwork"
    assert calls[-1]["headers"]["Authorization"] == "Token fw-key" and calls[-1]["params"]["location"]
    remote = SourceQuery(keyword="Python Developer", location="Remote", max_age_hours=24 * 7)
    aggregators.search_findwork(remote, "fw-key")
    assert calls[-1]["params"]["remote"] == "true" and "location" not in calls[-1]["params"]


def test_findwork_runs_as_a_keyed_source(http):
    routes, _ = http
    routes["findwork.dev"] = FINDWORK
    jobs = job_sources.scrape_all(["Python Developer"], ["Berlin"], ["findwork"], max_age_hours=24 * 7,
                                  user_id="u-f",
                                  key_lookup=lambda scope, platform_only=False: ResolvedKey("fw", "byok")
                                  if scope == "findwork" else None)
    assert {j["company"] for j in jobs} >= {"Beta"}


def test_catalog_has_the_new_sources():
    assert registry.get("working_nomads").kind == "feed" and registry.get("findwork").key_scopes == ("findwork",)
    db = SessionLocal()
    try:
        rows = {r.key: r for r in db.query(ProviderCatalog).filter(
            ProviderCatalog.key.in_(["working_nomads", "findwork"])).all()}
    finally:
        db.close()
    assert rows["working_nomads"].default_enabled is True and rows["working_nomads"].tier == "free"
    assert rows["findwork"].default_enabled is False and rows["findwork"].key_scope == "findwork"
