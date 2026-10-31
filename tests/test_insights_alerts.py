"""
P3.2 — match breakdown, freshness, LinkedIn applicant counts, HN "Who is
hiring?", Google Jobs via the user's own key, The Muse key, relocation and
encrypted EEO answers, and job alerts (CRUD, scheduler claim, run, digest).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import celery_tasks
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.alerts import JobAlert, JobAlertRun
from app.models.jobs import Job, UserJob
from app.models.profile import Profile
from app.providers.job_sources import aggregators, base, feeds, linkedin_applicants
from app.providers.job_sources.base import SourceQuery
from app.services import job_alerts, job_sources, job_store, pipeline_progress, provider_catalog, scorer
from app.services import search_profiles
from app.services.key_store import ResolvedKey

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "sources"


class FakeResponse:
    def __init__(self, body, status_code: int = 200):
        self._body, self.status_code = body, status_code
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body if not isinstance(self._body, str) else json.loads(self._body)


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


def _now_iso(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------- match breakdown

def test_match_breakdown_counts_skills_with_synonyms_and_title_fit():
    params = {"skill_terms": search_profiles.skill_terms(
        [{"name": "Python", "weight": "core", "synonyms": ["py"]}, {"name": "Kubernetes", "synonyms": ["k8s"]},
         {"name": "FastAPI"}], ["Redis"]),
        "career_targets": ["Backend Engineer", "Platform Engineer"]}
    job = {"title": "Senior Backend Engineer", "description": "We run py services on k8s with Postgres."}
    b = search_profiles.match_breakdown(job, params)
    assert b["skills_matched"] == ["Python", "Kubernetes"] and b["skills_missing"] == ["FastAPI", "Redis"]
    assert b["skills_total"] == 4 and b["title_fit"] == 100
    assert search_profiles.title_fit("Data Analyst", ["Backend Engineer"]) == 0
    assert search_profiles.title_fit("Anything", []) is None
    legacy = search_profiles.match_breakdown(job, {"skills": ["postgres", "go"], "career_targets": []})
    assert legacy["skills_matched"] == ["postgres"] and legacy["title_fit"] is None


def test_scorer_reads_optional_seniority_and_missing_required():
    jobs = [{"title": "a"}, {"title": "b"}]
    reply = json.dumps([
        {"index": 0, "score": 81, "reason": "ok", "seniority": "stretch", "missing_required": ["Go", "gRPC", "K8s", "x"]},
        {"index": 1, "score": 40, "reason": "meh", "seniority": "junior-ish"},
    ])
    assert scorer._parse_and_apply_scores(reply, jobs, lambda m: None)
    assert jobs[0]["llm_seniority"] == "stretch" and jobs[0]["llm_missing"] == ["Go", "gRPC", "K8s"]
    assert "llm_seniority" not in jobs[1] and jobs[1]["llm_score"] == 40  # unknown value ignored, score kept


# ---------------------------------------------------------------- freshness

def test_relative_and_absolute_dates_parse():
    assert abs((datetime.now(timezone.utc) - base.parse_date("3 days ago")).days - 3) <= 1
    assert base.parse_date("30+ days ago") is not None and base.parse_date("Just posted") is not None
    assert base.parse_date("2026-10-16T05:00:00Z").hour == 5


def test_freshness_flags():
    row = Job(job_key="k", posted_at=datetime.now(timezone.utc) - timedelta(hours=5),
              first_seen_at=datetime.now(timezone.utc), applicants="Over 200")
    f = job_store.freshness(row, previous_search_at=_now_iso(hours_ago=1))
    assert f["posted_hours_ago"] == 5 and f["early"] is True and f["new_since_last_search"] is True
    assert f["applicants"] == "Over 200"
    old = Job(job_key="k2", posted_at=datetime.now(timezone.utc) - timedelta(days=3),
              first_seen_at=datetime.now(timezone.utc) - timedelta(days=3))
    f = job_store.freshness(old, previous_search_at=_now_iso(hours_ago=1))
    assert f["early"] is False and f["new_since_last_search"] is False
    assert job_store.freshness(Job(job_key="k3"))["posted_at"] is None


def test_pipeline_feed_and_jobs_list_carry_match_and_freshness(client, make_user, monkeypatch):
    user_id, headers = make_user()
    job = {"source": "Indeed", "title": "Backend Engineer", "company": "Fresh Co", "location": "Pune",
           "url": "https://indeed.example/fresh-1", "description": "python fastapi " * 10, "posted": _now_iso(2)}
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [dict(job)])
    monkeypatch.setattr(celery_tasks, "_score_batch_llm", celery_tasks.one_by_one(lambda j, **kw: {
        **j, "llm_score": 88, "llm_reason": "fit", "llm_seniority": "match", "llm_missing": ["Kafka"]}))
    params = {"user_id": user_id, "resume_summary": "x", "skills": ["python", "django"],
              "career_targets": ["Backend Engineer"], "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 0,
              "filters": {}, "include_seen": False, "previous_search_at": _now_iso(1)}
    params["profile_hash"] = search_profiles.profile_hash(params)
    run_id = pipeline_progress.create_run(user_id=user_id, total_keywords=1)
    celery_tasks.search_and_score_keyword(run_id, "Backend", ["Pune"], ["indeed"], {}, params)

    entry = pipeline_progress.current_jobs(run_id=run_id)[0]
    assert entry["match"] == {"skills_matched": ["python"], "skills_missing": ["django"], "skills_total": 2,
                              "title_fit": 100, "seniority": "match", "missing_required": ["Kafka"]}
    assert entry["early"] is True and entry["posted_hours_ago"] == 2 and entry["new_since_last_search"] is True

    client.post(f"{API}/jobs/{entry['id']}/save", headers=headers)
    saved = client.get(f"{API}/jobs?state=saved", headers=headers).json()["jobs"][0]
    assert saved["match"]["seniority"] == "match" and saved["early"] is True and saved["posted_at"]


def test_previous_search_time_is_remembered_per_user():
    assert pipeline_progress.mark_user_search("u-fresh") is None
    first = pipeline_progress.mark_user_search("u-fresh")
    assert first and base.parse_date(first) is not None


# ---------------------------------------------------------------- LinkedIn applicant counts

@pytest.mark.parametrize("page,expected", [
    ('<span class="num-applicants__caption">\n143 applicants</span>', "143"),
    ("<figcaption>Over 200 applicants</figcaption>", "Over 200"),
    ("Be among the first 25 applicants", "Under 25"),
    ("<html>no count</html>", None),
])
def test_applicant_count_parsing(page, expected):
    assert linkedin_applicants.parse_count(page) == expected


def test_linkedin_job_id_from_url():
    assert linkedin_applicants.job_id("https://www.linkedin.com/jobs/view/4475781086") == "4475781086"
    assert linkedin_applicants.job_id("https://in.linkedin.com/jobs/view/python-dev-at-acme-4475781086?x=1") \
        == "4475781086"
    assert linkedin_applicants.job_id("https://indeed.com/viewjob?jk=1") is None


def test_applicant_counts_update_job_and_feed_and_stop_at_429(monkeypatch):
    db = SessionLocal()
    try:
        keys, run_id = [], pipeline_progress.create_run(user_id="u-app", total_keywords=1)
        for i in range(3):
            j = {"source": "LinkedIn", "title": f"Role {i}", "company": "Count Co", "location": "Pune",
                 "url": f"https://www.linkedin.com/jobs/view/90000000{i}", "description": "d"}
            key = celery_tasks.job_key(j)
            row = job_store.upsert_copy(db, j, key)
            payload = pipeline_progress.record_scored_job(run_id=run_id, job={**job_store.as_job(row)})
            pipeline_progress.remember_recorded(run_id=run_id, job_key=key, payload=payload)
            keys.append(key)
        answers = iter(["143", linkedin_applicants.RateLimited(), "9"])

        def fake_fetch(url):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a

        monkeypatch.setattr(linkedin_applicants, "fetch_count", fake_fetch)
        found = celery_tasks.add_applicant_counts(db, run_id, keys, sleep=lambda s: None)
        assert found == 1
        rows = [db.query(Job).filter(Job.job_key == k).one() for k in keys]
        assert rows[0].applicants == "143" and rows[1].applicants is None and rows[2].applicants_checked_at is None
        updated = [e for e in pipeline_progress.get_jobs(run_id=run_id) if e.get("replaces")]
        assert len(updated) == 1 and updated[0]["applicants"] == "143"
        # checked within 24 h: not fetched again
        monkeypatch.setattr(linkedin_applicants, "fetch_count", lambda url: pytest.fail("re-checked too soon"))
        celery_tasks.add_applicant_counts(db, None, keys[:1], sleep=lambda s: None)
    finally:
        db.close()


# ---------------------------------------------------------------- HN "Who is hiring?"

def test_hn_thread_is_found_and_parsed(http):
    routes, calls = http
    routes["search_by_date"] = json.loads((FIXTURES / "hn_search.json").read_text(encoding="utf-8"))
    routes["/items/"] = json.loads((FIXTURES / "hn_item.json").read_text(encoding="utf-8"))
    jobs = feeds.fetch_hn_hiring()
    assert "/items/49922569" in calls[-1]["url"]  # the "Who is hiring?" thread, not "Who wants to be hired?"
    assert len(jobs) >= 6
    by_company = {j["company"]: j for j in jobs}
    sidekick = by_company["Sidekick Labs"]
    assert "Engineer" in sidekick["title"] and "REMOTE (US Timezones)" in sidekick["location"]
    assert sidekick["url"].startswith("https://news.ycombinator.com/item?id=")
    assert base.geo_allows(sidekick["_geo"], "India") is False
    assert "PrairieLearn" in by_company  # en-dash header
    assert "Shepherd" not in by_company  # no role in the header -> skipped, not guessed


@pytest.mark.parametrize("header,company,title,india_ok", [
    ("Acme | Senior Backend Engineer | REMOTE | Full-time", "Acme", "Senior Backend Engineer", True),
    ("Acme | Data Scientist | Remote (India) | $40k", "Acme", "Data Scientist", True),
    ("Acme | ML Engineer | ONSITE | Berlin, Germany", "Acme", "ML Engineer", False),
    ("Acme (Remote-first) | Founding Engineer", "Acme", "Founding Engineer", True),
    ("We are hiring lots of people, come say hi", None, None, None),
])
def test_hn_header_parsing(header, company, title, india_ok):
    job = feeds._hn_parse({"id": 1, "created_at": _now_iso(), "text": f"{header}<p>Details here."})
    if company is None:
        assert job is None
        return
    assert job["company"] == company and job["title"] == title
    assert base.geo_allows(job["_geo"], "India") is india_ok


# ---------------------------------------------------------------- Google Jobs via the user's own key

SERPAPI = {"jobs_results": [{"title": "Python Developer", "company_name": "Acme", "location": "Bengaluru, India",
                             "description": "Django...", "detected_extensions": {"posted_at": "2 days ago"},
                             "apply_options": [{"title": "LinkedIn", "link": "https://lnkd.example/1"}]}]}
JSEARCH = {"status": "OK", "data": [{"job_title": "Backend Engineer", "employer_name": "Beta", "job_city": "Pune",
                                     "job_country": "IN", "job_apply_link": "https://beta.example/apply",
                                     "job_description": "Go...", "job_posted_at_datetime_utc": _now_iso(5),
                                     "job_is_remote": False}]}


def test_serpapi_and_jsearch_normalize(http):
    routes, calls = http
    routes["serpapi.com"], routes["jsearch.p.rapidapi.com"] = SERPAPI, JSEARCH
    q = SourceQuery(keyword="Python Developer", location="Bengaluru, India", max_age_hours=24 * 7)
    s = aggregators.search_serpapi(q, "serp-key")
    assert s[0]["source"] == "Google Jobs" and s[0]["url"] == "https://lnkd.example/1" and s[0]["posted"]
    assert calls[-1]["params"]["engine"] == "google_jobs" and calls[-1]["params"]["gl"] == "in"
    j = aggregators.search_jsearch(q, "rapid-key")
    assert j[0]["company"] == "Beta" and j[0]["location"] == "Pune, IN"
    assert calls[-1]["headers"]["X-RapidAPI-Key"] == "rapid-key" and calls[-1]["params"]["date_posted"] == "week"


def test_google_jobs_tries_providers_in_order(http, monkeypatch):
    routes, calls = http
    routes["api.brightdata.com"] = {"organic": []}  # unrecognised/empty reply -> next provider
    routes["serpapi.com"] = SERPAPI
    keys = {"brightdata": ResolvedKey("tok:zone", "byok"), "serpapi": ResolvedKey("serp", "byok"),
            "jsearch": ResolvedKey("rapid", "byok")}
    jobs = job_sources.scrape_all(["Python Developer"], ["Bengaluru"], ["google_jobs_api"], max_age_hours=24 * 7,
                                  user_id="u-g", key_lookup=lambda scope, platform_only=False: keys.get(scope))
    assert [j["title"] for j in jobs] == ["Python Developer"]
    assert [c["url"].split("/")[2] for c in calls] == ["api.brightdata.com", "serpapi.com"]  # JSearch not needed
    assert calls[0]["headers"]["Authorization"] == "Bearer tok" and calls[0]["json"]["zone"] == "zone"


def test_google_jobs_never_uses_a_shared_key_in_production(monkeypatch, client, make_user):
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production")
    assert provider_catalog.user_key_only("google_jobs_api") is True
    progress = []
    jobs = job_sources.scrape_all(["x"], ["Pune"], ["google_jobs_api"], progress=progress.append,
                                  key_lookup=lambda scope, platform_only=False: ResolvedKey("shared", "platform"))
    assert jobs == [] and any("no usable API key" in p for p in progress)
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "development")
    _, headers = make_user()
    view = {s["key"]: s for s in client.get(f"{API}/providers", headers=headers).json()["sources"]}
    assert view["google_jobs_api"]["enabled"] is False and view["google_jobs_api"]["tier"] == "free_key"
    assert view["hn_hiring"]["default_enabled"] is True


def test_admin_can_let_one_user_use_the_shared_google_jobs_keys(monkeypatch, client, make_user, make_admin, http):
    routes, calls = http
    routes["serpapi.com"] = SERPAPI
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production")
    allowed_id, allowed_headers = make_user()
    other_id, other_headers = make_user()
    _, admin_headers = make_admin()
    shared = lambda scope, platform_only=False: ResolvedKey("shared-serp", "platform") if scope == "serpapi" else None  # noqa: E731

    def run(user_id):
        return job_sources.scrape_all(["Python Developer"], ["Bengaluru"], ["google_jobs_api"], max_age_hours=24 * 7,
                                      user_id=user_id, key_lookup=shared)

    assert run(allowed_id) == []  # off by default
    r = client.put(f"{API}/admin/users/{allowed_id}/entitlements/shared_search_keys", headers=admin_headers,
                   json={"enabled": True})
    assert r.status_code == 200, r.text
    assert [j["title"] for j in run(allowed_id)] == ["Python Developer"]
    assert calls[-1]["params"]["api_key"] == "shared-serp"
    assert run(other_id) == []  # everyone else still needs their own key
    me = client.get(f"{API}/auth/me", headers=allowed_headers).json()
    assert me["entitlements"]["shared_search_keys"]["enabled"] is True


def test_the_muse_sends_the_platform_key(http):
    routes, calls = http
    routes["themuse.com"] = {"results": [], "page_count": 1}
    feeds.fetch_themuse("muse-key")
    assert calls[-1]["params"]["api_key"] == "muse-key"
    feeds.fetch_themuse()
    assert "api_key" not in calls[-1]["params"]


# ---------------------------------------------------------------- relocation + EEO

def test_relocation_and_eeo_answers_are_saved_and_eeo_is_encrypted(client, make_user):
    user_id, headers = make_user()
    body = {"headline": "Engineer", "applicant_info": {"willing_to_relocate": "depends",
                                                       "relocation_locations": ["Pune", "Hyderabad"],
                                                       "requires_sponsorship": False},
            "eeo": {"gender": "woman", "pronouns": "she/her"}}
    r = client.put(f"{API}/profile", json=body, headers=headers)
    assert r.status_code == 200, r.text
    out = client.get(f"{API}/profile", headers=headers).json()
    assert out["applicant_info"]["relocation_locations"] == ["Pune", "Hyderabad"]
    assert out["eeo"] == {"gender": "woman", "ethnicity": "prefer_not_to_say", "veteran_status": "prefer_not_to_say",
                          "disability_status": "prefer_not_to_say", "pronouns": "she/her"}
    db = SessionLocal()
    try:
        stored = db.query(Profile).filter(Profile.user_id == user_id).one()
        assert stored.eeo_encrypted and "woman" not in stored.eeo_encrypted
        assert "woman" not in json.dumps(stored.applicant_info)
    finally:
        db.close()
    # Leaving `eeo` out keeps the saved answers.
    client.put(f"{API}/profile", json={"headline": "Engineer II"}, headers=headers)
    assert client.get(f"{API}/profile", headers=headers).json()["eeo"]["gender"] == "woman"
    assert client.put(f"{API}/profile", json={"eeo": {"religion": "x"}}, headers=headers).status_code == 422


# ---------------------------------------------------------------- job alerts

PROFILE = {"name": "Backend", "titles": ["Backend Engineer"], "skills": [{"name": "Python", "weight": "core"}]}


def _profile(client, headers) -> str:
    return client.post(f"{API}/search-profiles", json=PROFILE, headers=headers).json()["id"]


def test_alert_crud_limits_and_validation(client, make_user):
    _, headers = make_user()
    _, other = make_user()
    pid = _profile(client, headers)
    r = client.post(f"{API}/alerts", json={"name": "Daily", "search_profile_id": pid}, headers=headers)
    assert r.status_code == 201, r.text
    alert = r.json()
    assert alert["min_score"] == 75 and alert["timezone"] == "Asia/Kolkata" and alert["hour_local"] == 9
    nxt = datetime.fromisoformat(alert["next_run_at"])
    assert (nxt.hour, nxt.minute // 60) == (3, 0) or nxt.hour == 4  # 09:mm IST = 03:30+mm UTC

    assert client.post(f"{API}/alerts", json={"name": "x", "search_profile_id": pid, "timezone": "Mars/Base"},
                       headers=headers).status_code == 422
    other_pid = _profile(client, other)
    assert client.post(f"{API}/alerts", json={"name": "x", "search_profile_id": other_pid},
                       headers=headers).json()["detail"]["code"] == "search_profile_not_found"
    for i in range(2):
        client.post(f"{API}/alerts", json={"name": f"A{i}", "search_profile_id": pid}, headers=headers)
    over = client.post(f"{API}/alerts", json={"name": "A4", "search_profile_id": pid}, headers=headers)
    assert over.status_code == 409 and over.json()["detail"]["code"] == "alert_limit"

    assert client.get(f"{API}/alerts", headers=other).json()["alerts"] == []
    assert client.put(f"{API}/alerts/{alert['id']}", json={"name": "x", "search_profile_id": pid},
                      headers=other).status_code == 404
    off = client.put(f"{API}/alerts/{alert['id']}",
                     json={"name": "Daily", "search_profile_id": pid, "enabled": False}, headers=headers).json()
    assert off["enabled"] is False and off["next_run_at"] is None
    assert client.delete(f"{API}/alerts/{alert['id']}", headers=headers).status_code == 204


def test_scheduler_claims_each_due_alert_once_and_dispatches(client, make_user, dispatched):
    _, headers = make_user()
    pid = _profile(client, headers)
    alert_id = client.post(f"{API}/alerts", json={"name": "Due", "search_profile_id": pid},
                           headers=headers).json()["id"]
    db = SessionLocal()
    try:
        row = db.get(JobAlert, alert_id)
        row.next_run_at = job_alerts.utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    dispatched.clear()
    assert celery_tasks.alerts_tick()["started"] >= 1
    assert any(c["task"] == "job_finder.run_job_alert" and c["args"] == [alert_id, "schedule"] for c in dispatched)
    db = SessionLocal()
    try:
        assert db.get(JobAlert, alert_id).next_run_at > job_alerts.utcnow()
        assert alert_id not in job_alerts.claim_due(db)  # already moved forward: nobody else gets it
    finally:
        db.close()
    assert job_alerts.last_tick()


def test_alert_run_emails_new_matches_once(client, make_user, monkeypatch):
    user_id, headers = make_user()
    pid = _profile(client, headers)
    # Resume uploads are rate-limited per client IP across the whole test session, so stub the context.
    monkeypatch.setattr(search_profiles, "resume_context", lambda db, uid: "Backend engineer with Python.")
    alert_id = client.post(f"{API}/alerts", json={"name": "Daily backend", "search_profile_id": pid,
                                                  "min_score": 80}, headers=headers).json()["id"]
    jobs = [{"source": "Indeed", "title": "Backend Engineer", "company": f"Alert Co {i}", "location": "Pune",
             "url": f"https://indeed.example/alert-{i}", "description": "python " * 20, "posted": _now_iso(3)}
            for i in range(3)]
    scores = {jobs[0]["url"]: 92, jobs[1]["url"]: 85, jobs[2]["url"]: 50}
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [dict(j) for j in jobs])
    monkeypatch.setattr(celery_tasks, "_score_batch_llm", celery_tasks.one_by_one(
                        lambda j, **kw: {**j, "llm_score": scores[j["url"]], "llm_reason": "fit"}))
    sent = []

    class Mailer:
        def send(self, *, to, subject, body, html=None):
            sent.append((to, subject, body))

    monkeypatch.setattr("app.providers.email.get_email_sender", lambda: Mailer())

    result = celery_tasks.run_job_alert(alert_id, "manual")
    assert result["new_jobs"] == 2, result
    assert len(sent) == 1 and "2 new job matches" in sent[0][1] and "Alert Co 0" in sent[0][2]
    assert "Alert Co 2" not in sent[0][2]

    runs = client.get(f"{API}/alerts/{alert_id}/runs", headers=headers).json()["runs"]
    assert runs[0]["status"] == "done" and runs[0]["new_jobs"] == 2 and runs[0]["emailed_at"]
    listed = client.get(f"{API}/alerts/runs/{runs[0]['id']}/jobs", headers=headers).json()["jobs"]
    assert [j["company"] for j in listed] == ["Alert Co 0", "Alert Co 1"] and listed[0]["llm_score"] == 92

    # Same jobs tomorrow: already shown, so no second email.
    result = celery_tasks.run_job_alert(alert_id, "schedule")
    assert result["new_jobs"] == 0 and len(sent) == 1


def test_alert_run_records_why_it_could_not_run(client, make_user, monkeypatch):
    user_id, headers = make_user()
    pid = _profile(client, headers)
    alert_id = client.post(f"{API}/alerts", json={"name": "No resume", "search_profile_id": pid},
                           headers=headers).json()["id"]
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    assert "error" in celery_tasks.run_job_alert(alert_id, "manual")
    run = client.get(f"{API}/alerts/{alert_id}/runs", headers=headers).json()["runs"][0]
    assert run["status"] == "failed" and "resume" in run["error"].lower()

    from app.models.user import User
    db = SessionLocal()
    try:
        db.get(User, user_id).is_active = False
        db.commit()
    finally:
        db.close()
    assert celery_tasks.run_job_alert(alert_id, "schedule") == {"skipped": "account disabled"}


def _alert_run_setup(client, headers, monkeypatch, scorer_fn) -> tuple[str, list]:
    """An alert over two fake Indeed jobs; returns its id and the list sent digests land in."""
    pid = _profile(client, headers)
    monkeypatch.setattr(search_profiles, "resume_context", lambda db, uid: "Backend engineer with Python.")
    alert_id = client.post(f"{API}/alerts", json={"name": "Daily", "search_profile_id": pid},
                           headers=headers).json()["id"]
    tag = alert_id[:8]
    jobs = [{"source": "Indeed", "title": "Backend Engineer", "company": f"Co {tag} {i}", "location": "Pune",
             "url": f"https://indeed.example/{tag}-{i}", "description": "python " * 20, "posted": _now_iso(3)}
            for i in range(2)]
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [dict(j) for j in jobs])
    monkeypatch.setattr(celery_tasks, "_score_batch_llm", celery_tasks.one_by_one(scorer_fn))
    sent = []

    class Mailer:
        def send(self, *, to, subject, body, html=None):
            sent.append((to, subject, body))

    monkeypatch.setattr("app.providers.email.get_email_sender", lambda: Mailer())
    return alert_id, sent


def test_alert_run_says_when_no_ai_model_could_score(client, make_user, monkeypatch):
    _, headers = make_user()
    alert_id, sent = _alert_run_setup(client, headers, monkeypatch, lambda j, **kw: {**j, "llm_failed": True})
    assert celery_tasks.run_job_alert(alert_id, "manual")["new_jobs"] == 0 and sent == []
    run = client.get(f"{API}/alerts/{alert_id}/runs", headers=headers).json()["runs"][0]
    assert run["status"] == "done" and run["counts"]["unscored"] == 2
    assert "2 jobs couldn't be scored" in run["error"] and "next run" in run["error"]

    # Next run the AI works: the same jobs are scored and sent (they were never marked seen).
    monkeypatch.setattr(celery_tasks, "_score_batch_llm", celery_tasks.one_by_one(
                        lambda j, **kw: {**j, "llm_score": 90, "llm_reason": "fit"}))
    assert celery_tasks.run_job_alert(alert_id, "schedule")["new_jobs"] == 2 and len(sent) == 1
    assert client.get(f"{API}/alerts/{alert_id}/runs", headers=headers).json()["runs"][0]["error"] is None


def _connect_gmail(user_id: str, address: str) -> None:
    from app.models.email import EmailAccount
    db = SessionLocal()
    try:
        db.add(EmailAccount(user_id=user_id, gmail_address=address, token_sealed="sealed"))
        db.commit()
    finally:
        db.close()


def _remove_gmail(user_id: str) -> None:
    from app.models.email import EmailAccount
    db = SessionLocal()
    try:
        db.query(EmailAccount).filter(EmailAccount.user_id == user_id).delete()
        db.commit()
    finally:
        db.close()


def test_alert_digest_goes_to_a_connected_gmail_or_the_sign_in_email(client, make_user, monkeypatch):
    from app.models.user import User
    user_id, headers = make_user()
    db = SessionLocal()
    try:
        login = db.get(User, user_id).email.lower()
    finally:
        db.close()
    alert_id, sent = _alert_run_setup(client, headers, monkeypatch,
                                      lambda j, **kw: {**j, "llm_score": 90, "llm_reason": "fit"})
    listed = client.get(f"{API}/alerts", headers=headers).json()
    assert listed["email_options"] == [login] and listed["alerts"][0]["sends_to"] == login
    pid = listed["alerts"][0]["search_profile_id"]
    body = {"name": "Daily", "search_profile_id": pid}

    # Only addresses the user proved they own.
    r = client.put(f"{API}/alerts/{alert_id}", json={**body, "email_to": "someone@else.com"}, headers=headers)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "alert_email_not_allowed"

    _connect_gmail(user_id, "Me.Jobs@gmail.com")
    assert client.get(f"{API}/alerts", headers=headers).json()["email_options"] == [login, "me.jobs@gmail.com"]
    out = client.put(f"{API}/alerts/{alert_id}", json={**body, "email_to": " ME.jobs@gmail.com"},
                     headers=headers).json()
    assert out["email_to"] == "me.jobs@gmail.com" and out["sends_to"] == "me.jobs@gmail.com"
    celery_tasks.run_job_alert(alert_id, "manual")
    assert [to for to, _, _ in sent] == ["me.jobs@gmail.com"]

    # Choosing the sign-in email stores "follow the sign-in email".
    out = client.put(f"{API}/alerts/{alert_id}", json={**body, "email_to": login}, headers=headers).json()
    assert out["email_to"] is None and out["sends_to"] == login

    # Gmail disconnected later: the digest falls back to the sign-in email.
    client.put(f"{API}/alerts/{alert_id}", json={**body, "email_to": "me.jobs@gmail.com"}, headers=headers)
    _remove_gmail(user_id)
    assert client.get(f"{API}/alerts", headers=headers).json()["alerts"][0]["sends_to"] == login
    db = SessionLocal()
    try:
        alert = db.get(JobAlert, alert_id)
        assert job_alerts.sends_to(alert, job_alerts.email_options(db, db.get(User, user_id))) == login
    finally:
        db.close()


def test_run_now_dispatches_for_the_owner_only(client, make_user, dispatched):
    _, headers = make_user()
    _, other = make_user()
    pid = _profile(client, headers)
    alert_id = client.post(f"{API}/alerts", json={"name": "Now", "search_profile_id": pid},
                           headers=headers).json()["id"]
    assert client.post(f"{API}/alerts/{alert_id}/run-now", headers=other).status_code == 404
    r = client.post(f"{API}/alerts/{alert_id}/run-now", headers=headers)
    assert r.status_code == 202 and r.json()["task_id"]
    assert dispatched[-1]["task"] == "job_finder.run_job_alert" and dispatched[-1]["args"] == [alert_id, "manual"]


def test_digest_text_lists_score_freshness_and_applicants():
    alert = JobAlert(name="Daily", min_score=75)
    subject, body = job_alerts.digest_text(alert, [
        {"title": "Backend Engineer", "company": "Acme", "location": "Pune", "llm_score": 91.0, "early": True,
         "applicants": "Over 200", "llm_reason": "Strong Python overlap.", "url": "https://x/1"}])
    assert subject == "1 new job match — Daily"
    assert "score 91" in body and "posted < 24 h ago" in body and "Over 200 applicants" in body
