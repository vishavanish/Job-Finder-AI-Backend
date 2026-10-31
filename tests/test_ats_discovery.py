"""
P6 — ATS Discovery: platform adapters (recorded fixtures for all 12
platforms), URL parsing + SSRF, the crawler (ETag/304, upsert, closing,
caps, switch-off, host slots), title search grouped by company (synonyms,
filters, hide applied, cache, top-up), the API, the `company_ats` pipeline
source and registry growth from apply links.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.db import SessionLocal
from app.models.ai import ProviderCatalog
from app.models.application import Application
from app.models.ats import AtsCompany, AtsJob, AtsPlatformSetting
from app.models.jobs import Job
from app.providers.ats import platforms
from app.providers.ats.platforms import AtsRef, GoneError
from app.providers.job_sources import base
from app.services import ats_crawler, ats_registry, ats_search, job_sources, provider_catalog
from tests.conftest import set_role

ATS = Path(__file__).parent / "fixtures" / "ats"
SOURCES = Path(__file__).parent / "fixtures" / "sources"


class FakeResponse:
    def __init__(self, body, status_code: int = 200, headers: dict | None = None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self._body) if isinstance(self._body, str) else self._body


def load(path: Path):
    text = path.read_text(encoding="utf-8")
    return text if path.suffix in (".xml", ".rss", ".html") else json.loads(text)


ROUTES = {
    "boards-api.greenhouse.io/v1/boards/airbnb/jobs/": ATS / "greenhouse_job.json",
    "boards-api.greenhouse.io/v1/boards/airbnb/jobs": ATS / "greenhouse.json",
    "api.lever.co/v0/postings/spotify": ATS / "lever.json",
    "api.smartrecruiters.com/v1/companies/BoschGroup/postings": ATS / "smartrecruiters.json",
    "nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs": ATS / "workday.json",
    "career.teamtailor.com/jobs.rss": ATS / "teamtailor.rss",
    "acme.teamtailor.com/jobs.rss": ATS / "teamtailor.rss",
    "ats.rippling.com/api/v2/board/rippling/jobs": ATS / "rippling.json",
    "api.gem.com/job_board/v0/gem/job_posts/": ATS / "gem.json",
    "bunq.recruitee.com/api/offers/": SOURCES / "recruitee.json",
    "api.ashbyhq.com/posting-api/job-board/ramp": SOURCES / "ashby.json",
    "apply.workable.com/api/v3/accounts/acme/jobs": SOURCES / "workable.json",
    "acme.jobs.personio.de/xml": SOURCES / "personio.xml",
    "acme.breezy.hr/json": SOURCES / "breezy.json",
    "steeloncall.keka.com/careers/api/jobs/default/active": ATS / "keka.json",
    "steeloncall.keka.com/careers/api/organization/default/careerportalinfo": ATS / "keka_info.json",
    "digitalnest.zohorecruit.in/jobs/Careers": ATS / "zoho_recruit.html",
}


@pytest.fixture
def http(monkeypatch):
    """Routes adapter HTTP to recorded fixtures by URL substring (longest match first); records calls."""
    overrides: dict[str, FakeResponse] = {}
    calls: list[dict] = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        for part in sorted(overrides, key=len, reverse=True):
            if part in url:
                return overrides[part]
        for part in sorted(ROUTES, key=len, reverse=True):
            if part in url:
                return FakeResponse(load(ROUTES[part]), headers={"ETag": 'W/"v1"'})
        return FakeResponse({"error": "not routed"}, 404)

    monkeypatch.setattr(base, "request", fake)
    return overrides, calls


@pytest.fixture(autouse=True)
def clean_ats():
    db = SessionLocal()
    try:
        db.query(AtsJob).delete()
        db.query(AtsCompany).delete()
        db.query(AtsPlatformSetting).update({AtsPlatformSetting.enabled: True})
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def company(db, ref: AtsRef, name: str | None = None) -> AtsCompany:
    row, _ = ats_registry.get_or_create(db, ref, origin="admin", name=name)
    return row


# ---------------------------------------------------------------- adapters

@pytest.mark.parametrize("ref,min_jobs", [
    (AtsRef("greenhouse", "airbnb"), 3),
    (AtsRef("lever", "spotify"), 2),
    (AtsRef("recruitee", "bunq"), 1),
    (AtsRef("ashby", "ramp"), 1),
    (AtsRef("smartrecruiters", "BoschGroup"), 2),
    (AtsRef("workday", "nvidia", host="nvidia.wd5.myworkdayjobs.com", site="NVIDIAExternalCareerSite"), 3),
    (AtsRef("workable", "acme"), 1),
    (AtsRef("personio", "acme"), 1),
    (AtsRef("breezy", "acme"), 1),
    (AtsRef("teamtailor", "teamtailor", host="career.teamtailor.com"), 2),
    (AtsRef("rippling", "rippling"), 2),
    (AtsRef("gem", "gem"), 2),
    (AtsRef("keka", "steeloncall"), 3),
    (AtsRef("zoho_recruit", "digitalnest", host="zohorecruit.in"), 4),
])
def test_every_platform_parses_its_recorded_response(http, monkeypatch, ref, min_jobs):
    monkeypatch.setattr(platforms, "public_host", lambda host: True)
    result = platforms.get(ref.platform).list_jobs(ref, max_jobs=100, markets=("India",))
    assert len(result.jobs) >= min_jobs
    for job in result.jobs:
        assert job["external_id"] and job["title"] and job["url"].startswith("https://")
        assert set(job) >= {"location", "department", "posted", "remote", "country", "description"}


def test_greenhouse_sends_etag_and_honours_304(http):
    overrides, calls = http
    ref = AtsRef("greenhouse", "airbnb")
    first = platforms.get("greenhouse").list_jobs(ref)
    assert first.etag == 'W/"v1"' and first.company_name == "Airbnb"
    overrides["boards-api.greenhouse.io/v1/boards/airbnb/jobs"] = FakeResponse("", 304)
    again = platforms.get("greenhouse").list_jobs(ref, etag=first.etag)
    assert again.not_modified and calls[-1]["headers"]["If-None-Match"] == 'W/"v1"'


def test_redirect_or_404_means_the_company_left_the_platform(http):
    overrides, _ = http
    overrides["api.lever.co/v0/postings/gone"] = FakeResponse("", 302)
    with pytest.raises(GoneError):
        platforms.get("lever").list_jobs(AtsRef("lever", "gone"))
    assert http[1][-1]["allow_redirects"] is False


def test_workday_big_tenant_indexes_target_market_first(http):
    overrides, calls = http
    page = load(ATS / "workday.json")
    page["total"] = 5000
    india = {"total": 2, "jobPostings": [
        {"title": "SDE India", "externalPath": "/job/India-Bengaluru/SDE_JR1", "locationsText": "India, Bengaluru",
         "postedOn": "Posted Today", "bulletFields": ["JR1"]}]}

    def fake(method, url, **kwargs):
        calls.append({"url": url, **kwargs})
        body = india if kwargs.get("json", {}).get("searchText") == "India" else page
        return FakeResponse(body)
    import app.providers.job_sources.base as b
    b.request = fake  # restored by monkeypatch in the http fixture
    ref = AtsRef("workday", "nvidia", host="nvidia.wd5.myworkdayjobs.com", site="NVIDIAExternalCareerSite")
    result = platforms.get("workday").list_jobs(ref, max_jobs=5, markets=("India",))
    assert not result.complete
    assert result.jobs[0]["title"] == "SDE India" and result.jobs[0]["posted"] == "today"
    assert len(result.jobs) <= 5


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/stripe/jobs/123", ("greenhouse", "stripe", None, None)),
    ("https://boards.greenhouse.io/embed/job_app?for=gitlab&token=1", ("greenhouse", "gitlab", None, None)),
    ("https://jobs.lever.co/palantir/abc/apply", ("lever", "palantir", None, None)),
    ("https://jobs.ashbyhq.com/openai/xyz", ("ashby", "openai", None, None)),
    ("https://bunq.recruitee.com/o/backend", ("recruitee", "bunq", None, None)),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x",
     ("workday", "nvidia", "nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite")),
    ("https://jobs.smartrecruiters.com/BoschGroup/7440001", ("smartrecruiters", "BoschGroup", None, None)),
    ("https://apply.workable.com/acme/j/ABC123/", ("workable", "acme", None, None)),
    ("https://acme.jobs.personio.de/job/1", ("personio", "acme", None, None)),
    ("https://acme.breezy.hr/p/1", ("breezy", "acme", None, None)),
    ("https://acme.teamtailor.com/jobs/1", ("teamtailor", "acme", None, None)),
    ("https://ats.rippling.com/rippling/jobs/abc", ("rippling", "rippling", None, None)),
    ("https://jobs.gem.com/gem/1", ("gem", "gem", None, None)),
    ("https://techdome.keka.com/careers/jobdetails/168376?source=indeed", ("keka", "techdome", None, None)),
    ("https://kanerika.zohorecruit.com/jobs/Careers/705962000023064981/Engineer-Backend?source=CareerSite",
     ("zoho_recruit", "kanerika", "zohorecruit.com", None)),
    ("https://digitalnest.zohorecruit.in/jobs/Careers", ("zoho_recruit", "digitalnest", "zohorecruit.in", None)),
])
def test_urls_map_to_platform_refs(url, expected):
    ref = platforms.parse_url(url)
    assert (ref.platform, ref.slug, ref.host, ref.site) == expected


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/1", "https://example.com/careers", "https://evil.myworkdayjobs.com.attacker.io/x",
    "https://jobs.lever.co/", "https://acme.homerun.co/", "https://bad_slug!.recruitee.com/", "not a url",
    "https://acme.keka.com/#/home", "https://www.keka.com/careers", "https://www.zohorecruit.com/jobs/Careers",
    "https://acme.zohorecruit.com.attacker.io/jobs/Careers",
])
def test_unsupported_or_malformed_urls_are_refused(url):
    assert platforms.parse_url(url) is None


def test_public_host_blocks_private_addresses(monkeypatch):
    monkeypatch.setattr(platforms.socket, "getaddrinfo", lambda h, p: [(0, 0, 0, "", ("169.254.169.254", 443))])
    assert not platforms.public_host("careers.acme.com")
    monkeypatch.setattr(platforms.socket, "getaddrinfo", lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 443))])
    assert platforms.public_host("careers.acme.com")
    assert not platforms.public_host("localhost")


# ---------------------------------------------------------------- crawler

def test_crawl_indexes_then_304_then_closes_removed_jobs(http, db):
    overrides, _ = http
    c = company(db, AtsRef("greenhouse", "airbnb"))
    res = ats_crawler.crawl_company(db, c.id)
    assert res["added"] == 3
    db.refresh(c)
    assert c.job_count == 3 and c.etag == 'W/"v1"' and c.name == "Airbnb" and c.next_crawl_at > c.last_ok_at
    jobs = db.query(AtsJob).filter(AtsJob.company_id == c.id).all()
    assert all(j.snippet == "" and j.job_key.startswith("airbnb|") for j in jobs)

    overrides["boards-api.greenhouse.io/v1/boards/airbnb/jobs"] = FakeResponse("", 304)
    assert ats_crawler.crawl_company(db, c.id) == {"company_id": c.id, "not_modified": True}

    page = load(ATS / "greenhouse.json")
    page["jobs"] = page["jobs"][:1]
    overrides["boards-api.greenhouse.io/v1/boards/airbnb/jobs"] = FakeResponse(page, headers={"ETag": "v2"})
    res = ats_crawler.crawl_company(db, c.id)
    assert res["closed"] == 2
    db.refresh(c)
    assert c.job_count == 1


def test_company_switched_off_after_repeated_404s(http, db):
    overrides, _ = http
    overrides["api.lever.co/v0/postings/plaid"] = FakeResponse({"ok": False}, 404)
    c = company(db, AtsRef("lever", "plaid"))
    for _ in range(3):
        ats_crawler.crawl_company(db, c.id)
    db.refresh(c)
    assert not c.active and c.gone_count == 3


def test_errors_back_off_and_record_platform_health(http, db):
    overrides, _ = http
    overrides["api.lever.co/v0/postings/spotify"] = FakeResponse({}, 503)
    c = company(db, AtsRef("lever", "spotify"))
    res = ats_crawler.crawl_company(db, c.id)
    assert "503" in res["error"]
    db.refresh(c)
    assert c.active and c.fail_count == 1 and c.next_crawl_at > datetime.now(timezone.utc).replace(tzinfo=None)
    assert db.get(AtsPlatformSetting, "lever").consecutive_failures >= 1


def test_host_slot_limits_parallel_crawls(http, db, fake_redis):
    c = company(db, AtsRef("lever", "spotify"))
    fake_redis.set("ats:host:api.lever.co", 5)
    assert ats_crawler.crawl_company(db, c.id)["skipped"] == "host busy"
    assert db.query(AtsJob).count() == 0


def test_cap_keeps_target_markets_first():
    jobs = [{"title": f"t{i}", "location": "Berlin", "posted": f"2026-10-{10 + i:02d}"} for i in range(5)]
    jobs.append({"title": "india", "location": "Pune, India", "posted": "2026-01-01"})
    jobs.append({"title": "remote", "location": "Remote", "remote": True, "posted": "2026-01-02"})
    kept, complete = ats_crawler.cap_jobs(jobs, 3, ("India", "Remote"))
    assert not complete and [j["title"] for j in kept] == ["remote", "india", "t4"]


def test_claim_due_claims_each_company_once(db):
    a = company(db, AtsRef("lever", "a1"))
    company(db, AtsRef("lever", "a2"))
    first = ats_crawler.claim_due(db)
    assert a.id in first and ats_crawler.claim_due(db) == []


def test_disabled_platform_is_not_crawled(http, db):
    c = company(db, AtsRef("lever", "spotify"))
    db.get(AtsPlatformSetting, "lever").enabled = False
    db.commit()
    assert ats_crawler.crawl_company(db, c.id)["skipped"] == "platform off"


def test_closed_jobs_are_purged_after_retention(http, db):
    c = company(db, AtsRef("lever", "spotify"))
    ats_crawler.crawl_company(db, c.id)
    db.query(AtsJob).update({AtsJob.closed_at: datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)})
    db.commit()
    assert ats_crawler.purge_closed(db) == 2


# ---------------------------------------------------------------- search

def seed_index(db, rows: list[tuple[str, str, str, dict]]):
    """rows: (platform, slug, title, extra job fields)."""
    for platform, slug, title, extra in rows:
        c = company(db, AtsRef(platform, slug), name=slug.title())
        job = {"external_id": f"{slug}-{title}", "title": title, "location": extra.get("location", "Bengaluru, India"),
               "department": "", "url": f"https://jobs.example/{slug}/{title}", "posted": extra.get("posted", ""),
               "remote": extra.get("remote", False), "country": "", "description": "Build things"}
        ats_crawler.upsert_jobs(db, c, [job], complete=False, close=False)
        c.last_ok_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()


def test_search_groups_by_company_and_expands_synonyms(db, make_user):
    user_id, _ = make_user()
    seed_index(db, [
        ("greenhouse", "acme", "Senior Software Engineer", {}),
        ("greenhouse", "acme", "Software Developer II", {}),
        ("lever", "globex", "Software Developer", {"remote": True, "location": "Remote"}),
        ("workable", "initech", "Product Manager", {}),
    ])
    result = ats_search.search(db, user_id, "Sr. SDE")
    assert result["expansions"][0] == "software engineer" and "software developer" in result["expansions"]
    names = {g["name"]: g for g in result["groups"]}
    assert set(names) == {"Acme", "Globex"}
    assert names["Acme"]["matching_jobs"] == 2 and names["Acme"]["platform_name"] == "Greenhouse"
    assert result["registry"]["companies"] == 3 and result["total_jobs"] == 3
    hot = db.query(AtsCompany).filter(AtsCompany.name == "Acme").one().hot_until
    assert hot and hot > datetime.now(timezone.utc).replace(tzinfo=None)


def test_search_filters_platform_remote_location_posted_and_applied(db, make_user):
    user_id, _ = make_user()
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    seed_index(db, [
        ("greenhouse", "acme", "Data Engineer", {"location": "Pune, India", "posted": old}),
        ("lever", "globex", "Data Engineer", {"remote": True, "location": "Remote"}),
        ("ashby", "hooli", "Big Data Engineer", {"location": "Berlin, Germany"}),
    ])
    s = lambda **kw: {g["name"] for g in ats_search.search(db, user_id, "data engineer", **kw)["groups"]}  # noqa: E731
    assert s() == {"Acme", "Globex", "Hooli"}
    assert s(platforms_=["lever", "ashby"]) == {"Globex", "Hooli"}
    assert s(remote=True) == {"Globex"}
    assert s(location="india") == {"Acme"}
    assert s(posted_within_days=30) == {"Globex", "Hooli"}
    key = db.query(AtsJob).join(AtsCompany).filter(AtsCompany.name == "Hooli").one().job_key
    db.add(Application(user_id=user_id, job_url="https://x", job_key=key, job_title="Big Data Engineer",
                       company="Hooli", source="Ashby", status="submitted"))
    db.commit()
    assert s(hide_applied=True) == {"Acme", "Globex"}
    hooli = [g for g in ats_search.search(db, user_id, "data engineer", page=1)["groups"] if g["name"] == "Hooli"]
    assert hooli[0]["top_jobs"][0]["applied"] is True


def test_disabled_platform_is_left_out_of_search(db, make_user):
    user_id, _ = make_user()
    seed_index(db, [("lever", "globex", "QA Engineer", {})])
    db.get(AtsPlatformSetting, "lever").enabled = False
    db.commit()
    assert ats_search.search(db, user_id, "qa engineer")["groups"] == []


def test_ai_expansion_is_cached_for_the_next_search(db, monkeypatch, make_user):
    from app.services import ai_router
    user_id, _ = make_user()
    calls = []

    def fake(db_, **kw):
        calls.append(kw)
        assert kw["task"] == "title_suggest"
        return type("R", (), {"text": json.dumps(["Growth Hacker", "Acquisition Marketer!!", "x" * 80])})()
    monkeypatch.setattr(ai_router, "complete", fake)
    phrases, known = ats_search.expansions("Growth Marketer")
    assert phrases == ["growth marketer"] and not known
    assert ats_search.expand_with_ai(db, "Growth Marketer", user_id) == ["Growth Hacker"]
    phrases, known = ats_search.expansions("growth marketer")
    assert known and phrases == ["growth marketer", "growth hacker"]


# ---------------------------------------------------------------- API

def test_search_api_needs_the_entitlement_and_starts_a_topup(client, make_user, dispatched, db):
    user_id, headers = make_user()
    seed_index(db, [("greenhouse", "acme", "Backend Engineer", {})])
    resp = client.get("/api/v1/ats/search", params={"q": "backend developer"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"][0]["name"] == "Acme" and body["topup_task_id"]
    assert any(c["task"] == "job_finder.ats_topup" for c in dispatched)
    again = client.get("/api/v1/ats/search", params={"q": "backend developer"}, headers=headers).json()
    assert again["topup_task_id"] is None  # one top-up per query per 10 minutes

    from app.services import entitlements
    from app.models.admin import UserEntitlement
    db.add(UserEntitlement(user_id=user_id, feature="ats_discovery", enabled=False))
    db.commit()
    assert client.get("/api/v1/ats/search", params={"q": "backend"}, headers=headers).status_code == 403
    assert entitlements.effective(db, user_id, "ats_discovery").enabled is False


def test_add_company_by_link_is_shared_and_crawled(client, make_user, dispatched, db):
    _, headers = make_user()
    resp = client.post("/api/v1/ats/companies", json={"url": "https://jobs.lever.co/spotify/abc"}, headers=headers)
    assert resp.status_code == 201 and resp.json()["created"] and resp.json()["platform"] == "lever"
    assert dispatched[-1]["task"] == "job_finder.crawl_ats_company"
    again = client.post("/api/v1/ats/companies", json={"url": "jobs.lever.co/spotify"}, headers=headers)
    assert again.status_code == 201 and again.json()["created"] is False
    bad = client.post("/api/v1/ats/companies", json={"url": "https://example.com/careers"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "ats_unsupported"
    assert client.post("/api/v1/ats/companies", json={"platform": "workday", "slug": "x"},
                       headers=headers).status_code == 422


def test_job_detail_and_track_make_a_normal_job(client, make_user, http, db):
    _, headers = make_user()
    c = company(db, AtsRef("greenhouse", "airbnb"))
    ats_crawler.crawl_company(db, c.id)
    job = db.query(AtsJob).filter(AtsJob.company_id == c.id).first()
    detail = client.get(f"/api/v1/ats/jobs/{job.id}", headers=headers).json()
    assert detail["description"] and detail["job_id"] is None and detail["platform"] == "greenhouse"
    tracked = client.post(f"/api/v1/ats/jobs/{job.id}/track", headers=headers).json()
    row = db.get(Job, tracked["job_id"])
    assert row.best_source == "Greenhouse" and row.apply_url == job.url
    options = client.get(f"/api/v1/apply/options/{row.id}", headers=headers)
    assert options.status_code == 200


def test_admin_routes_are_admin_only_and_audited(client, make_user, db):
    user_id, headers = make_user()
    assert client.get("/api/v1/admin/ats/health", headers=headers).status_code == 403
    set_role(user_id, "admin")
    health = client.get("/api/v1/admin/ats/health", headers=headers).json()
    assert {p["platform"] for p in health["platforms"]} == set(platforms.PLATFORMS)
    resp = client.patch("/api/v1/admin/ats/platforms/gem", json={"enabled": False}, headers=headers)
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    from app.models.admin import AuditLog
    assert db.query(AuditLog).filter(AuditLog.action == "ats_platform.update").count() >= 1


# ---------------------------------------------------------------- pipeline source & growth

def test_company_ats_source_feeds_scrape_all(db):
    seed_index(db, [("greenhouse", "acme", "Python Developer", {"location": "Bengaluru, Karnataka, India"}),
                    ("lever", "globex", "Python Developer", {"location": "London, UK"}),
                    ("ashby", "hooli", "Python Engineer", {"remote": True, "location": "Remote"})])
    lookup = lambda kw: ats_search.index_jobs(db, kw, locations=["Bangalore"], country="India",  # noqa: E731
                                              max_age_hours=24, limit=10, detail_budget=0)
    jobs = job_sources.scrape_all(["python developer"], ["Bangalore"], ["company_ats"], index_jobs=lookup,
                                  progress=lambda m: None)
    assert {(j["company"], j["source"]) for j in jobs} == {("Acme", "Greenhouse"), ("Hooli", "Ashby")}
    assert all(j["apply_url"] == j["url"] for j in jobs)


def test_company_ats_source_needs_the_entitlement(db, make_user):
    from app.models.admin import UserEntitlement
    from app.models.user import User
    user_id, _ = make_user()
    user = db.get(User, user_id)
    row = db.get(ProviderCatalog, "company_ats")
    assert provider_catalog.availability(db, user, row) == (True, None)
    db.add(UserEntitlement(user_id=user_id, feature="ats_discovery", enabled=False))
    db.commit()
    assert provider_catalog.availability(db, user, row)[0] is False


def test_apply_links_grow_the_registry(db):
    jobs = [{"url": "https://www.linkedin.com/jobs/view/1", "apply_url": "https://job-boards.greenhouse.io/figma/jobs/1"},
            {"url": "https://jobs.lever.co/kraken/abc"}, {"url": "https://example.com/x"},
            {"url": "https://job-boards.greenhouse.io/figma/jobs/2"}]
    assert ats_registry.harvest(db, jobs) == 2
    assert {c.origin for c in db.query(AtsCompany).all()} == {"pipeline"}
    assert ats_registry.harvest(db, jobs) == 0


def test_seed_file_loads_once(db, tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps([{"platform": "lever", "slug": "spotify", "name": "Spotify"},
                                {"url": "https://bunq.recruitee.com/"}, {"platform": "nope", "slug": "x"}]))
    assert ats_registry.seed(db, path) == 2
    assert ats_registry.seed(db, path) == 0
    assert db.query(AtsCompany).filter(AtsCompany.name == "Spotify").one().origin == "seed"


def test_shipped_seed_file_is_valid():
    entries = json.loads(ats_registry.SEED_FILE.read_text(encoding="utf-8"))
    assert entries
    for e in entries:
        impl = platforms.get(e["platform"])
        assert impl and impl.valid(AtsRef(e["platform"], e["slug"], host=e.get("host"), site=e.get("site")))


# ---------------------------------------------------------------- Postgres speed (CI job with a Postgres service)

@pytest.mark.skipif(not os.environ.get("ATS_PG_URL"), reason="needs ATS_PG_URL (Postgres) — runs in the CI pg job")
def test_postgres_search_under_three_seconds_on_300k_jobs():
    """Acceptance (CLAUDE.md §15 P6): title query → grouped companies in < 3 s from the index."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(os.environ["ATS_PG_URL"])
    Session = sessionmaker(bind=engine)
    s = Session()
    titles = ["Software Engineer", "Data Engineer", "Product Manager", "Sales Executive", "Backend Developer",
              "Designer", "Accountant", "HR Generalist", "Support Engineer", "QA Engineer"]
    if s.execute(text("SELECT count(*) FROM ats_jobs")).scalar() < 300000:
        s.execute(text("DELETE FROM ats_jobs")); s.execute(text("DELETE FROM ats_companies"))  # noqa: E702
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        s.execute(text(
            "INSERT INTO ats_companies (id, ref_key, platform, slug, name, careers_url, origin, active, job_count, "
            "fail_count, gone_count, created_at) SELECT 'c' || g, 'greenhouse:c' || g, "
            "(ARRAY['greenhouse','lever','workday','recruitee','ashby'])[1 + g % 5], 'c' || g, 'Company ' || g, "
            "'https://x', 'seed', true, 100, 0, 0, :now FROM generate_series(1, 3000) g"), {"now": now})
        s.execute(text(
            "INSERT INTO ats_jobs (id, company_id, external_id, title, title_norm, location, country, remote, "
            "department, url, snippet, job_key, content_hash, posted_at, first_seen_at, last_seen_at) "
            "SELECT 'j' || g, 'c' || (1 + (g / 10) % 3000), 'e' || g, t.title || ' ' || (g % 7), lower(t.title), "
            "'Bengaluru, India', '', g % 4 = 0, 'Eng', 'https://x/' || g, 'Build things with python and sql', "
            "'k' || g, 'h', :now - (g % 90) * interval '1 day', :now, :now "
            "FROM generate_series(1, 300000) g JOIN (SELECT unnest(:titles) AS title, "
            "generate_series(0, 9) AS i) t ON t.i = g % 10"), {"now": now, "titles": titles})
        s.commit()
    s.execute(text("ANALYZE ats_jobs"))
    start = time.perf_counter()
    result = ats_search._search(s, "nobody", ["software engineer", "software developer"], filters={},
                                platforms_=None, location=None, remote=None, posted_within_days=None,
                                hide_applied=False, page=1)
    elapsed = time.perf_counter() - start
    assert result["total_companies"] > 100 and elapsed < 3.0, elapsed


# ---------------------------------------------------------------- QA2 §4

def _seed_company(db, platform: str, slug: str, jobs: list[dict]):
    c = company(db, AtsRef(platform, slug), name=slug.title())
    rows = [{"external_id": f"{slug}-{i}", "title": j["title"], "location": j.get("location", ""),
             "department": "", "url": f"https://jobs.example/{slug}/{i}", "posted": j.get("posted", ""),
             "remote": j.get("remote", False), "country": "", "description": "Build things"}
            for i, j in enumerate(jobs)]
    ats_crawler.upsert_jobs(db, c, rows, complete=False, close=False)
    c.last_ok_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


def test_same_role_in_many_places_is_one_row_with_more_locations(db, make_user):
    user_id, _ = make_user()
    _seed_company(db, "ashby", "bjak", [
        {"title": "Machine Learning Engineer, Applied AI", "location": "Germany (Remote)"},
        {"title": "Machine Learning Engineer, Applied AI", "location": "Ireland (Remote)"},
        {"title": "machine learning engineer, applied ai", "location": "China (Remote)"},
        {"title": "Senior Machine Learning Engineer", "location": "Germany (Remote)"},
    ])
    group = ats_search.search(db, user_id, "machine learning engineer")["groups"][0]
    rows = {j["title"]: j for j in group["top_jobs"]}
    assert len(group["top_jobs"]) == 2
    applied_ai = rows["Machine Learning Engineer, Applied AI"]
    assert sorted([applied_ai["location"], *applied_ai["more_locations"]]) == [
        "China (Remote)", "Germany (Remote)", "Ireland (Remote)"]


def test_several_locations_and_remote_match_any_of_them(db, make_user):
    user_id, _ = make_user()
    seed_index(db, [
        ("greenhouse", "acme", "Data Engineer", {"location": "Mumbai, India"}),
        ("lever", "globex", "Data Engineer", {"remote": True, "location": "Anywhere"}),
        ("ashby", "hooli", "Data Engineer", {"location": "Berlin, Germany"}),
    ])
    names = lambda **kw: {g["name"] for g in ats_search.search(db, user_id, "data engineer", **kw)["groups"]}  # noqa: E731
    assert names(location=["Mumbai", "Remote"]) == {"Acme", "Globex"}
    assert names(location="India, Remote") == {"Acme", "Globex"}
    assert names(location=["berlin"]) == {"Hooli"}


def test_old_postings_are_left_out_unless_any_age(db, make_user):
    user_id, _ = make_user()
    stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    seed_index(db, [
        ("greenhouse", "acme", "Platform Engineer", {"posted": stale}),
        ("lever", "globex", "Platform Engineer", {}),
    ])
    names = lambda **kw: {g["name"] for g in ats_search.search(db, user_id, "platform engineer", **kw)["groups"]}  # noqa: E731
    assert names() == {"Globex"}
    assert names(any_age=True) == {"Acme", "Globex"}


def test_search_api_takes_several_locations_and_any_age(client, make_user, db):
    _, headers = make_user()
    seed_index(db, [("greenhouse", "acme", "Site Reliability Engineer", {"location": "Pune, India"})])
    r = client.get("/api/v1/ats/search", headers=headers,
                   params=[("q", "site reliability engineer"), ("location", "Pune"), ("location", "Remote"),
                           ("any_age", "true")])
    assert r.status_code == 200, r.text
    assert [g["name"] for g in r.json()["groups"]] == ["Acme"]
    assert r.json()["groups"][0]["top_jobs"][0]["more_locations"] == []


# ---------------------------------------------------------------- Keka / Zoho Recruit (QA7)

def test_keka_jobs_carry_city_country_date_and_the_company_name(http):
    ref = AtsRef("keka", "steeloncall")
    impl = platforms.get("keka")
    result = impl.list_jobs(ref)
    job = result.jobs[0]
    assert job["country"] == "India" and job["posted"].startswith("20") and job["description"]
    assert job["url"] == f"https://steeloncall.keka.com/careers/jobdetails/{job['external_id']}"
    assert impl.board_name(ref, result.jobs) == "Steel On Call Services (India) Private Limited"


def test_zoho_recruit_reads_the_embedded_list_and_skips_unpublished(http):
    result = platforms.get("zoho_recruit").list_jobs(AtsRef("zoho_recruit", "digitalnest", host="zohorecruit.in"))
    assert result.company_name == "Digital Nest"
    assert all("/jobs/Careers/" in j["url"] and j["location"].startswith("Hyderabad") for j in result.jobs)
    overrides, _ = http
    page = load(ATS / "zoho_recruit.html").replace("&quot;Publish&quot;: true", "&quot;Publish&quot;: false", 1)
    overrides["digitalnest.zohorecruit.in/jobs/Careers"] = FakeResponse(page)
    fewer = platforms.get("zoho_recruit").list_jobs(AtsRef("zoho_recruit", "digitalnest", host="zohorecruit.in"))
    assert len(fewer.jobs) == len(result.jobs) - 1


def test_unknown_keka_or_zoho_company_means_gone(http):
    overrides, _ = http
    overrides["nosuch.keka.com"] = FakeResponse("", 302)
    overrides["nosuch.zohorecruit.com"] = FakeResponse('<html><div class="cl-error-block">Oops</div></html>')
    overrides["quiet.zohorecruit.com"] = FakeResponse("<html><title>Jobs at Quiet</title><body>No openings</body></html>")
    with pytest.raises(GoneError):
        platforms.get("keka").list_jobs(AtsRef("keka", "nosuch"))
    with pytest.raises(GoneError):
        platforms.get("zoho_recruit").list_jobs(AtsRef("zoho_recruit", "nosuch", host="zohorecruit.com"))
    quiet = platforms.get("zoho_recruit").list_jobs(AtsRef("zoho_recruit", "quiet", host="zohorecruit.com"))
    assert quiet.jobs == [] and quiet.company_name == "Quiet"


def test_seed_is_loaded_again_when_a_new_platform_has_no_companies(db, tmp_path, monkeypatch):
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps([{"platform": "lever", "slug": "spotify"},
                                     {"platform": "keka", "slug": "techdome"}]), encoding="utf-8")
    monkeypatch.setattr(ats_registry, "SEED_FILE", seed_file)
    assert ats_registry.needs_seed(db)  # empty registry
    company(db, AtsRef("lever", "spotify"))
    assert ats_registry.needs_seed(db)  # keka is in the seed but not in the registry
    assert ats_registry.seed(db) == 1
    assert not ats_registry.needs_seed(db)


def test_zoho_location_has_no_repeats_and_site_name_beats_the_title():
    impl = platforms.get("zoho_recruit")
    assert impl._location({"City": "Hyderabad, Ahmedabad, India", "Country": "India"}) == "Hyderabad, Ahmedabad, India"
    assert impl._location({"City": "hyderabad", "State": "Telangana", "Country": "India"}) == \
        "Hyderabad, Telangana, India"
    page = ('<title>Jobs at PeoplePlus</title><meta property="og:site_name" content="Kanerika Software">')
    assert impl._company(page) == "Kanerika Software"
    assert impl._company("<title>Jobs at Digital Nest</title>") == "Digital Nest"
