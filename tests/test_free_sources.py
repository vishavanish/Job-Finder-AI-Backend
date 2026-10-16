"""
P3.1 — free job sources: adapters (offline, recorded fixtures), feed cache,
keys and quotas, source health and auto-pause, default/remote source
selection, dedup rank, and the new company-ATS fetchers.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ai import ProviderCatalog
from app.providers.job_sources import aggregators, base, feeds, registry
from app.providers.job_sources.base import SourceError, SourceQuery
from app.services import company_ats_sources, job_dedupe, job_sources, job_store, scorer, source_limits
from app.services.key_store import ResolvedKey

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
YEARS = 24 * 365 * 5  # fixture dates are fixed; ignore their age unless a test is about age


class FakeResponse:
    def __init__(self, body, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def fixture(name: str):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text if name.endswith((".xml", ".rss")) else json.loads(text)


@pytest.fixture
def http(monkeypatch):
    """Routes adapter HTTP calls to fixtures by URL substring; records calls."""
    routes: dict[str, object] = {}
    calls: list[dict] = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        for part, body in routes.items():
            if part in url:
                return body if isinstance(body, FakeResponse) else FakeResponse(body)
        return FakeResponse({"error": "not routed"}, 404)

    monkeypatch.setattr(base, "request", fake)
    monkeypatch.setattr(company_ats_sources.requests, "request", fake)
    return routes, calls


# ---------------------------------------------------------------- adapters

FEED_FIXTURES = {
    "remoteok": ("remoteok.com", "remoteok.json"),
    "remotive": ("remotive.com", "remotive.json"),
    "himalayas": ("himalayas.app", "himalayas.json"),
    "jobicy": ("jobicy.com", "jobicy.json"),
    "themuse": ("themuse.com", "themuse.json"),
    "wwr": ("weworkremotely.com", "wwr.rss"),
}


@pytest.mark.parametrize("key", sorted(FEED_FIXTURES))
def test_feed_adapters_normalize_recorded_payloads(http, key):
    routes, _ = http
    host, name = FEED_FIXTURES[key]
    routes[host] = fixture(name)
    jobs = feeds.FEEDS[key]()
    assert jobs, key
    for job in jobs:
        assert job["source"] == registry.get(key).name
        assert job["title"] and job["url"].startswith("http")
        assert "<" not in job["description"]
        assert "_geo" in job and "_tags" in job


def test_arbeitnow_keeps_only_remote_jobs(http):
    routes, _ = http
    data = fixture("arbeitnow.json")
    data["data"][0]["remote"], data["data"][1]["remote"] = True, False
    routes["arbeitnow.com"] = data
    jobs = feeds.fetch_arbeitnow()
    titles = {j["title"] for j in jobs}
    assert data["data"][1]["title"] not in titles and len(jobs) >= 1
    assert all(j["_geo"].endswith("only") for j in jobs)  # EU-bound, so not shown to India users


def test_adzuna_maps_country_and_needs_app_id_and_key(http):
    routes, calls = http
    routes["api.adzuna.com"] = fixture("adzuna.json")
    q = SourceQuery(keyword="Python Developer", location="Bengaluru, India", country="India", max_age_hours=48)
    jobs = aggregators.search_adzuna(q, "my-app-id:my-app-key")
    assert [j["title"] for j in jobs] == ["Python Developer", "Data Engineer"]
    assert jobs[0]["company"] == "Acme Pvt Ltd" and "FastAPI" in jobs[0]["description"]
    assert "/jobs/in/search/1" in calls[-1]["url"]
    assert calls[-1]["params"]["where"] == "Bengaluru" and calls[-1]["params"]["max_days_old"] == 2
    with pytest.raises(SourceError, match="APP_ID:APP_KEY"):
        aggregators.search_adzuna(q, "only-one-part")
    with pytest.raises(SourceError, match="not covered"):
        aggregators.search_adzuna(SourceQuery(keyword="x", location="Lagos", country="Nigeria"), "a:b")


def test_jooble_posts_keyword_and_drops_old_postings(http):
    routes, calls = http
    data = fixture("jooble.json")
    now = datetime.now(timezone.utc)
    data["jobs"][0]["updated"] = (now - timedelta(hours=2)).isoformat()
    data["jobs"][1]["updated"] = (now - timedelta(days=30)).isoformat()
    routes["jooble.org/api/"] = data
    jobs = aggregators.search_jooble(SourceQuery(keyword="Python", location="Remote", max_age_hours=24), "jooble-key")
    assert [j["title"] for j in jobs] == ["Senior Python Engineer"]
    assert calls[-1]["method"] == "POST" and calls[-1]["json"]["keywords"] == "Python remote"


def test_careerjet_uses_basic_auth_locale_and_client_details(http):
    routes, calls = http
    routes["search.api.careerjet.net"] = fixture("careerjet.json")
    q = SourceQuery(keyword="Python", location="Mumbai, India", max_age_hours=YEARS,
                    client_ip="203.0.113.7", client_user_agent="Mozilla/5.0 test")
    jobs = aggregators.search_careerjet(q, "cj-key")
    assert jobs[0]["title"] == "Backend Developer (Python)" and jobs[0]["location"] == "Mumbai"
    call = calls[-1]
    assert call["auth"] == ("cj-key", "")
    assert call["params"]["locale_code"] == "en_IN" and call["params"]["user_ip"] == "203.0.113.7"


def test_http_errors_become_source_errors_without_the_url(http):
    routes, _ = http
    routes["jooble.org/api/"] = FakeResponse({"error": "bad key"}, 403)
    with pytest.raises(SourceError) as exc:
        aggregators.search_jooble(SourceQuery(keyword="x", location="Pune"), "secret-key-123")
    assert "403" in str(exc.value) and "secret-key-123" not in str(exc.value)


# ---------------------------------------------------------------- scrape_all

def _feed_job(title, geo="Worldwide", posted=None, source="RemoteOK"):
    posted = posted or datetime.now(timezone.utc).isoformat()
    return {"source": source, "title": title, "company": "Co", "location": "Remote", "url": f"https://x/{title}",
            "description": "", "posted": posted, "_geo": geo, "_tags": ""}


@pytest.mark.parametrize("title,tags,keyword,expected", [
    ("Senior Python Developer", "", "Python Developer", True),
    ("Sr React Native", "engineer backend developer", "Python Developer", False),  # role words alone don't match
    ("Product Design Director", "engineer", "Backend Engineer", False),
    ("Staff Backend Engineer (Go)", "", "Backend Engineer", True),
    ("Platform Engineer", "python aws", "Python Developer", True),  # specific word in tags
    ("Data Engineer", "", "Engineer", True),
    ("Marketing Lead", "", "Engineer", False),
])
def test_keyword_matching_needs_the_specific_words(title, tags, keyword, expected):
    assert base.matches_keyword({"title": title, "description": ""}, keyword, tags) is expected


def test_description_alone_counts_only_for_multi_word_keywords():
    job = {"title": "Platform Engineer", "description": "We use Python, Django and Postgres every day."}
    assert base.matches_keyword(job, "Python Developer") is False
    assert base.matches_keyword(job, "Python Django Developer") is True


@pytest.mark.parametrize("restriction,allowed", [
    ("", True), ("Worldwide", True), ("Anywhere in the World", True), ("India", True), ("APAC", True),
    ("United Kingdom", False), ("USA only", False), ("Remote - US only", False), ("Berlin only", False),
])
def test_region_filter_for_india(restriction, allowed):
    assert base.geo_allows(restriction, "India") is allowed


def test_feed_is_downloaded_once_and_filtered_by_keyword_region_and_age(monkeypatch):
    fetches = []
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

    def fake_feed():
        fetches.append(1)
        return [_feed_job("Senior Python Developer"), _feed_job("Python Engineer", geo="USA only"),
                _feed_job("Product Designer"), _feed_job("Python Developer (old)", posted=old)]

    monkeypatch.setitem(feeds.FEEDS, "remoteok", fake_feed)
    for _ in range(2):
        jobs = job_sources.scrape_all(["Python Developer"], ["Remote"], ["remoteok"], max_age_hours=48)
        assert [j["title"] for j in jobs] == ["Senior Python Developer"]
        assert not any(k.startswith("_") for k in jobs[0])
    assert len(fetches) == 1


def test_failing_source_is_reported_and_others_still_return(monkeypatch):
    def broken():
        raise SourceError("RemoteOK: HTTP 503")

    monkeypatch.setitem(feeds.FEEDS, "remoteok", broken)
    monkeypatch.setitem(feeds.FEEDS, "jobicy", lambda: [_feed_job("Python Developer", source="Jobicy")])
    results: dict[str, str | None] = {}
    jobs = job_sources.scrape_all(["Python Developer"], ["Remote"], ["remoteok", "jobicy"],
                                  on_source_result=lambda k, e: results.__setitem__(k, e))
    assert [j["source"] for j in jobs] == ["Jobicy"]
    assert results == {"remoteok": "RemoteOK: HTTP 503", "jobicy": None}

    # Straight after a failure the feed is skipped, not retried by every keyword task, and not re-counted.
    results.clear()
    job_sources.scrape_all(["Python"], ["Remote"], ["remoteok"], on_source_result=lambda k, e: results.__setitem__(k, e))
    assert results == {}


def test_keyed_api_needs_a_key_and_respects_the_daily_cap(monkeypatch):
    calls = []
    monkeypatch.setitem(aggregators.KEYWORD_APIS, "adzuna", lambda q, key: calls.append((q.keyword, key)) or [])
    job_sources.scrape_all(["Python"], ["Pune"], ["adzuna"], key_lookup=lambda scope: None)
    assert calls == []

    monkeypatch.setattr(get_settings(), "SOURCE_DAILY_CAP_PLATFORM_KEY", 1)
    lookup = lambda scope: ResolvedKey("app:key", "platform")  # noqa: E731
    job_sources.scrape_all(["Python", "Django"], ["Pune"], ["adzuna"], user_id="u1", key_lookup=lookup)
    assert calls == [("Python", "app:key")]

    # The user's own key has its own, higher cap.
    job_sources.scrape_all(["Django"], ["Pune"], ["adzuna"], user_id="u1",
                           key_lookup=lambda scope: ResolvedKey("own:key", "byok"))
    assert calls[-1] == ("Django", "own:key")


def test_cached_feed_waits_for_a_parallel_download(fake_redis):
    fake_redis.set("feedlock:remotive", "1")
    with pytest.raises(source_limits.FeedUnavailable):
        source_limits.cached_feed("remotive", lambda: [], wait_seconds=0.6)


# ---------------------------------------------------------------- API: which sources run

def _search(client, headers, dispatched, **body):
    resp = client.post("/api/v1/search", json={"keywords": ["Python Developer"], **body}, headers=headers)
    return resp, (dispatched[-1]["args"][0] if resp.status_code == 202 else None)


def test_default_sources_are_every_free_source_that_can_run(client, make_user, dispatched):
    _, headers = make_user()
    resp, params = _search(client, headers, dispatched, locations=["Pune, India"])
    assert resp.status_code == 202
    assert params["sources"] == ["linkedin", "indeed"]  # no keys, not remote; Glassdoor/Google off by default
    assert params["skipped_sources"] == []

    resp, params = _search(client, headers, dispatched, locations=["Remote"])
    assert params["sources"] == ["linkedin", "indeed", "remoteok", "remotive", "himalayas", "jobicy", "wwr"]


def test_remote_work_mode_in_preferences_turns_feeds_on(client, make_user, dispatched):
    _, headers = make_user()
    assert client.put("/api/v1/preferences", json={"work_modes": ["remote"]}, headers=headers).status_code == 200
    _, params = _search(client, headers, dispatched, locations=["Pune, India"])
    assert "remoteok" in params["sources"]


def test_free_key_source_runs_with_the_users_key_which_is_never_enqueued(client, make_user, dispatched):
    _, headers = make_user()
    key = "my-adzuna-id:my-adzuna-secret"
    assert client.post("/api/v1/keys", json={"scope": "adzuna", "key": key}, headers=headers).status_code == 201
    resp, params = _search(client, headers, dispatched, locations=["Pune, India"],
                           sources=["adzuna", "careerjet"])
    assert resp.status_code == 202 and params["sources"] == ["adzuna"]
    assert any("Careerjet: needs a careerjet API key" in n for n in params["skipped_sources"])
    assert "my-adzuna-secret" not in json.dumps(dispatched)


def test_careerjet_gets_the_callers_ip_and_user_agent(client, make_user, dispatched, monkeypatch):
    monkeypatch.setattr(get_settings(), "CAREERJET_API_KEY", "platform-careerjet-key")
    _, headers = make_user()
    _, params = _search(client, headers, dispatched, locations=["Mumbai"], sources=["careerjet"])
    assert params["sources"] == ["careerjet"] and params["client_ip"] and params["client_user_agent"]
    assert "platform-careerjet-key" not in json.dumps(dispatched)


def test_explicit_remote_feed_without_remote_search_says_why(client, make_user, dispatched):
    _, headers = make_user()
    resp, _ = _search(client, headers, dispatched, locations=["Pune"], sources=["remoteok"])
    assert resp.status_code == 422
    assert "remote-only" in resp.json()["detail"]["message"]


def test_unknown_source_and_deprecated_naukri_auto(client, make_user, dispatched):
    _, headers = make_user()
    resp, _ = _search(client, headers, dispatched, sources=["monster"])
    assert resp.status_code == 422
    resp, params = _search(client, headers, dispatched, sources=["linkedin"], enable_naukri_auto=True)
    assert resp.status_code == 202 and params["enable_naukri_auto"] is False
    assert any("captcha" in n for n in params["skipped_sources"])


def test_failing_source_is_paused_then_retried(client, make_user, dispatched):
    _, headers = make_user()
    db = SessionLocal()
    try:
        row = db.get(ProviderCatalog, "linkedin")
        row.consecutive_failures, row.last_error_at = 5, datetime.now(timezone.utc)
        db.commit()
        _, params = _search(client, headers, dispatched, sources=["linkedin", "indeed"])
        assert params["sources"] == ["indeed"]
        assert any("LinkedIn: paused after repeated errors" in n for n in params["skipped_sources"])
        view = {s["key"]: s for s in client.get("/api/v1/providers", headers=headers).json()["sources"]}
        assert view["linkedin"]["available"] is False

        row.last_error_at = datetime.now(timezone.utc) - timedelta(hours=2)  # cooldown over
        db.commit()
        _, params = _search(client, headers, dispatched, sources=["linkedin", "indeed"])
        assert params["sources"] == ["linkedin", "indeed"]
    finally:
        row.consecutive_failures, row.last_error_at = 0, None
        db.commit()
        db.close()


def test_worker_records_source_health(monkeypatch):
    from app.core import celery_tasks

    def fake_scrape_all(**kw):
        kw["on_source_result"]("remoteok", None)
        kw["on_source_result"]("jobicy", "Jobicy: HTTP 500")
        assert kw["key_lookup"]("adzuna") is None  # resolved in the worker, from the key store
        return []

    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", fake_scrape_all)
    db = SessionLocal()
    try:
        celery_tasks._scrape(db, {"user_id": None}, keywords=["x"], locations=["Remote"],
                             sources=["remoteok", "jobicy"], manual_naukri_jobs=[], progress=lambda m: None)
        ok, bad = db.get(ProviderCatalog, "remoteok"), db.get(ProviderCatalog, "jobicy")
        assert ok.last_success_at is not None and ok.consecutive_failures == 0
        assert bad.consecutive_failures == 1 and bad.last_error == "Jobicy: HTTP 500"
    finally:
        for key in ("remoteok", "jobicy"):
            row = db.get(ProviderCatalog, key)
            row.consecutive_failures, row.last_error, row.last_error_at, row.last_success_at = 0, None, None, None
        db.commit()
        db.close()


def test_providers_page_lists_new_sources_with_defaults(client, make_user):
    _, headers = make_user()
    view = {s["key"]: s for s in client.get("/api/v1/providers", headers=headers).json()["sources"]}
    assert set(registry.SOURCES) <= set(view)
    assert view["glassdoor"]["enabled"] is False and view["glassdoor"]["default_enabled"] is False
    assert view["adzuna"]["tier"] == "free_key" and view["adzuna"]["reason"] == "needs a adzuna API key"
    assert view["remoteok"]["available"] is True and view["remoteok"]["health"]["paused_until"] is None


# ---------------------------------------------------------------- dedup rank

def _copy(source, description="x"):
    return {"source": source, "title": "Backend Engineer", "company": "Acme", "location": "Pune",
            "url": f"https://{source}.example/job", "description": description}


def test_dedup_rank_linkedin_then_company_ats_then_boards_then_aggregators():
    long = "y" * 500
    assert job_dedupe.preferred(_copy("Adzuna", long), _copy("Greenhouse"))["source"] == "Greenhouse"
    assert job_dedupe.preferred(_copy("Greenhouse", long), _copy("LinkedIn"))["source"] == "LinkedIn"
    assert job_dedupe.preferred(_copy("RemoteOK", long), _copy("Indeed"))["source"] == "Indeed"
    assert job_dedupe.preferred(_copy("Indeed"), _copy("Glassdoor", long))["source"] == "Glassdoor"


def test_canonical_job_follows_the_rank():
    db = SessionLocal()
    try:
        key = job_dedupe.job_key(_copy("Adzuna"))
        assert job_store.upsert_copy(db, _copy("Adzuna", "a" * 900), key).best_source == "Adzuna"
        assert job_store.upsert_copy(db, _copy("Greenhouse", "short"), key).best_source == "Greenhouse"
        assert job_store.upsert_copy(db, _copy("Indeed", "i" * 2000), key).best_source == "Greenhouse"
        row = job_store.upsert_copy(db, _copy("LinkedIn", "l"), key)
        assert row.best_source == "LinkedIn" and row.best_source_rank == 0
        assert set(job_store.sources(db, [row.id])[row.id]) == {"Adzuna", "Greenhouse", "Indeed", "LinkedIn"}
    finally:
        db.close()


def test_scoring_prompt_marks_aggregator_snippets():
    prompt = scorer._build_user_prompt([_copy("Adzuna"), _copy("LinkedIn")], "resume")
    assert prompt.count("short snippet") == 1


# ---------------------------------------------------------------- company ATS

ATS_FIXTURES = {
    "ashby": ("api.ashbyhq.com/posting-api/job-board/ramp", "ashby.json", "ramp"),
    "recruitee": ("bunq.recruitee.com/api/offers/", "recruitee.json", "bunq"),
    "workable": ("apply.workable.com/api/v3/accounts/huggingface/jobs", "workable.json", "huggingface"),
    "personio": ("personio.jobs.personio.de/xml", "personio.xml", "personio"),
    "breezy": ("breezy.breezy.hr/json", "breezy.json", "breezy"),
}


@pytest.mark.parametrize("platform", sorted(ATS_FIXTURES))
def test_company_ats_fetchers(http, platform):
    routes, calls = http
    url_part, name, slug = ATS_FIXTURES[platform]
    routes[url_part] = fixture(name)
    jobs = company_ats_sources.scrape_company_pages([{"platform": platform, "slug": slug}], request_delay_sec=0)
    assert jobs and all(j["title"] and j["url"].startswith("https://") for j in jobs)
    assert jobs[0]["source"] == platform.title()
    assert calls[0]["allow_redirects"] is False


def test_company_ats_slug_cannot_change_the_host(http):
    _, calls = http
    msgs = []
    for platform in ("recruitee", "personio", "breezy", "ashby", "workable"):
        company_ats_sources.scrape_company_pages([{"platform": platform, "slug": "evil.com#"}],
                                                 request_delay_sec=0, progress=msgs.append)
    assert calls == []
    assert sum("skipping" in m for m in msgs) == 5


def test_workday_platform_is_dispatched_and_ssrf_guarded(client, make_user, monkeypatch):
    seen = []
    monkeypatch.setattr(company_ats_sources, "fetch_workday",
                        lambda url, slug, progress=None: seen.append((url, slug)) or [])
    company_ats_sources.scrape_company_pages(
        [{"platform": "workday", "slug": "nvidia",
          "custom_endpoint_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"}],
        request_delay_sec=0)
    assert seen == [("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "nvidia")]

    _, headers = make_user()
    body = {"targets": [{"platform": "workday", "slug": "x", "custom_endpoint_url": "http://127.0.0.1/careers"}]}
    assert client.post("/api/v1/company-ats/fetch", json=body, headers=headers).status_code == 400
