"""QA4: Target companies — follow companies, faster checks, new-role hits, email digests, suggestions,
follow by name and the ★ on Jobs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import celery_tasks
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.admin import UserEntitlement
from app.models.ats import (
    AtsCompany, AtsFollow, AtsFollowHit, AtsFollowSettings, AtsJob, AtsPlatformSetting, AtsProbe,
)
from app.models.profile import SearchProfile
from app.models.user import User
from app.providers.ats.platforms import AtsRef, ListResult
from app.services import ats_crawler, ats_registry, ats_resolver, pipeline_progress, target_companies

API = "/api/v1"


@pytest.fixture(autouse=True)
def clean():
    db = SessionLocal()
    try:
        for model in (AtsFollowHit, AtsFollow, AtsFollowSettings, AtsProbe, AtsJob, AtsCompany):
            db.query(model).delete()
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


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def company(db, slug: str, name: str | None = None, platform: str = "ashby") -> AtsCompany:
    row, _ = ats_registry.get_or_create(db, AtsRef(platform, slug), origin="admin", name=name or slug.title())
    return row


def add_jobs(db, row: AtsCompany, jobs: list[tuple[str, str]], *, remote: bool = False) -> list[str]:
    """(title, location) pairs -> new open jobs; returns their ids."""
    new: list = []
    ats_crawler.upsert_jobs(db, row, [
        {"external_id": f"{row.slug}-{t}-{loc}", "title": t, "location": loc, "department": "",
         "url": f"https://jobs.example/{row.slug}/{t}/{loc}", "posted": "", "remote": remote, "country": "",
         "description": ""} for t, loc in jobs], complete=False, close=False, new_rows=new)
    db.commit()
    return [r.id for r in new]


def titles_for(db, user_id: str, titles: list[str]) -> None:
    db.add(SearchProfile(user_id=user_id, name="Main", is_default=True, titles=titles, skills=[]))
    db.commit()


def follow_api(client, headers, company_id: str):
    return client.put(f"{API}/ats/companies/{company_id}/follow", headers=headers)


# ---------------------------------------------------------------- follow / unfollow

def test_follow_makes_the_company_refresh_often_and_unfollow_undoes_it(client, make_user, db):
    user_id, headers = make_user()
    row = company(db, "acme")
    row.next_crawl_at = _now() + timedelta(hours=20)
    db.commit()
    r = follow_api(client, headers, row.id)
    assert r.status_code == 200 and r.json()["status"] == "following" and r.json()["name"] == "Acme"
    db.refresh(row)
    assert row.follower_count == 1 and row.next_crawl_at <= _now() + timedelta(seconds=5)
    assert ats_crawler._next_crawl(row, _now()) <= _now() + timedelta(hours=3.5)
    assert follow_api(client, headers, row.id).status_code == 200  # following twice is fine
    assert client.delete(f"{API}/ats/companies/{row.id}/follow", headers=headers).status_code == 204
    db.refresh(row)
    assert row.follower_count == 0
    assert client.delete(f"{API}/ats/companies/{row.id}/follow", headers=headers).status_code == 404


def test_follow_limit(client, make_user, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "TARGET_COMPANIES_MAX_PER_USER", 1)
    _, headers = make_user()
    a, b = company(db, "acme"), company(db, "globex")
    assert follow_api(client, headers, a.id).status_code == 200
    r = follow_api(client, headers, b.id)
    assert r.status_code == 409 and "up to 1" in r.json()["detail"]


def test_following_needs_the_entitlement(client, make_user, db):
    user_id, headers = make_user()
    db.add(UserEntitlement(user_id=user_id, feature="ats_discovery", enabled=False))
    db.commit()
    assert client.get(f"{API}/ats/following", headers=headers).status_code == 403


# ---------------------------------------------------------------- new roles

def test_new_matching_roles_become_hits_for_followers_only(client, make_user, db):
    alice, alice_h = make_user()
    bob, _ = make_user()
    titles_for(db, alice, ["Backend Engineer"])
    db.add(AtsFollowSettings(user_id=alice, alert_mode="daily", titles=[], places=["Bengaluru", "Remote"],
                             timezone="Asia/Kolkata"))
    db.commit()
    row = company(db, "acme")
    follow_api(client, alice_h, row.id)
    ids = add_jobs(db, row, [("Senior Backend Developer", "Bengaluru, India"), ("Backend Engineer", "London"),
                             ("Sales Executive", "Bengaluru")])
    assert target_companies.match_new_jobs(db, row.id, ids) == 1  # synonyms count; London and Sales don't
    assert db.query(AtsFollowHit).filter(AtsFollowHit.user_id == bob).count() == 0
    listing = client.get(f"{API}/ats/following", headers=alice_h).json()
    assert listing["companies"][0]["new_count"] == 1 and listing["companies"][0]["open_matching"] == 1
    roles = client.get(f"{API}/ats/following/new", headers=alice_h).json()["roles"]
    assert [r["title"] for r in roles] == ["Senior Backend Developer"] and roles[0]["seen"] is False
    assert client.post(f"{API}/ats/following/seen", json={}, headers=alice_h).json()["marked"] == 1
    assert client.get(f"{API}/ats/following", headers=alice_h).json()["companies"][0]["new_count"] == 0


def test_a_crawl_that_adds_jobs_to_a_followed_company_queues_the_match(client, make_user, db, monkeypatch,
                                                                      dispatched):
    _, headers = make_user()
    row = company(db, "acme")
    follow_api(client, headers, row.id)
    monkeypatch.setattr(ats_crawler, "fetch", lambda c: ListResult(jobs=[
        {"external_id": "1", "title": "Backend Engineer", "location": "Bengaluru", "department": "",
         "url": "https://jobs.example/1", "posted": "", "remote": False, "country": "", "description": ""}]))
    ats_crawler.crawl_company(db, row.id)
    sent = [c for c in dispatched if c["task"] == "job_finder.ats_follow_match"]
    assert len(sent) == 1 and sent[0]["args"][0] == row.id and len(sent[0]["args"][1]) == 1
    ats_crawler.crawl_company(db, row.id)  # nothing new the second time
    assert len([c for c in dispatched if c["task"] == "job_finder.ats_follow_match"]) == 1


# ---------------------------------------------------------------- digest

def _hit(db, user_id: str, row: AtsCompany, title: str = "Backend Engineer") -> None:
    (job_id,) = add_jobs(db, row, [(title, "Bengaluru")])
    db.add(AtsFollowHit(user_id=user_id, company_id=row.id, ats_job_id=job_id))
    db.commit()


def test_instant_digest_at_most_hourly(make_user, db):
    user_id, _ = make_user()
    db.add(AtsFollowSettings(user_id=user_id, alert_mode="instant", titles=[], places=[], timezone="Asia/Kolkata"))
    db.commit()
    row = company(db, "razorpay", "Razorpay")
    _hit(db, user_id, row)
    sent: list[dict] = []
    now = _now()
    counts = target_companies.digest(db, send=lambda **kw: sent.append(kw), now=now)
    assert counts["emails"] == 1 and sent[0]["subject"] == "New at Razorpay: Backend Engineer"
    assert "https://jobs.example/razorpay/Backend Engineer/Bengaluru" in sent[0]["body"]
    _hit(db, user_id, row, "Frontend Engineer")
    target_companies.digest(db, send=lambda **kw: sent.append(kw), now=now + timedelta(minutes=20))
    assert len(sent) == 1
    target_companies.digest(db, send=lambda **kw: sent.append(kw), now=now + timedelta(minutes=61))
    assert len(sent) == 2 and "Frontend Engineer" in sent[1]["body"] and "Backend" not in sent[1]["body"]


def test_daily_digest_waits_for_nine_local(make_user, db):
    user_id, _ = make_user()
    row = company(db, "acme")
    _hit(db, user_id, row)  # no settings row: daily, Asia/Kolkata
    sent: list[dict] = []
    early = datetime(2026, 10, 26, 2, 0)   # 07:30 IST
    late = datetime(2026, 10, 26, 4, 0)    # 09:30 IST
    target_companies.digest(db, send=lambda **kw: sent.append(kw), now=early)
    assert sent == []
    target_companies.digest(db, send=lambda **kw: sent.append(kw), now=late)
    assert len(sent) == 1
    _hit(db, user_id, row, "Data Engineer")
    target_companies.digest(db, send=lambda **kw: sent.append(kw), now=late + timedelta(hours=3))
    assert len(sent) == 1  # once a day


def test_off_closed_and_disabled_users_get_no_email(make_user, db):
    off_user, _ = make_user()
    closed_user, _ = make_user()
    disabled, _ = make_user()
    for uid in (off_user, closed_user, disabled):
        db.add(AtsFollowSettings(user_id=uid, alert_mode="off" if uid == off_user else "instant", titles=[],
                                 places=[], timezone="Asia/Kolkata"))
    db.commit()
    a, b, c = company(db, "a1"), company(db, "b1"), company(db, "c1")
    _hit(db, off_user, a)
    _hit(db, closed_user, b)
    _hit(db, disabled, c)
    db.query(AtsJob).filter(AtsJob.company_id == b.id).update({AtsJob.closed_at: _now()})
    db.get(User, disabled).is_active = False
    db.commit()
    sent: list[dict] = []
    target_companies.digest(db, send=lambda **kw: sent.append(kw))
    assert sent == []


# ---------------------------------------------------------------- follow by name

def test_follow_a_known_name_at_once(client, make_user, db):
    _, headers = make_user()
    row = company(db, "razorpay", "Razorpay")
    r = client.post(f"{API}/ats/following/by-name", json={"name": "Razorpay Software Pvt Ltd"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "following"
    assert r.json()["follow"]["company_id"] == row.id and r.json()["task_id"] is None


def test_an_unknown_name_is_looked_up_then_followed(client, make_user, db, dispatched, monkeypatch):
    user_id, headers = make_user()
    r = client.post(f"{API}/ats/following/by-name", json={"name": "Initech"}, headers=headers)
    body = r.json()
    assert body["status"] == "searching" and body["task_id"]
    assert [c for c in dispatched if c["task"] == "job_finder.ats_follow_lookup"]

    def found(item, enabled):
        return AtsRef("greenhouse", "initech"), "Initech", "slug", True

    monkeypatch.setattr(ats_resolver, "resolve_one", found)
    assert target_companies.lookup_name(SessionLocal, body["follow"]["id"]) == {"status": "following"}
    listing = client.get(f"{API}/ats/following", headers=headers).json()["companies"]
    assert listing[0]["status"] == "following" and listing[0]["platform"] == "greenhouse"


def test_a_name_not_found_waits_and_attaches_when_the_company_turns_up(client, make_user, db, monkeypatch):
    _, headers = make_user()
    body = client.post(f"{API}/ats/following/by-name", json={"name": "Hooli"}, headers=headers).json()
    monkeypatch.setattr(ats_resolver, "resolve_one", lambda item, enabled: (None, None, None, True))
    assert target_companies.lookup_name(SessionLocal, body["follow"]["id"]) == {"status": "not_found"}
    assert client.get(f"{API}/ats/following", headers=headers).json()["companies"][0]["status"] == "not_found"
    # Later a search shows a Hooli job and the resolver finds its board.
    monkeypatch.setattr(ats_resolver, "resolve_one",
                        lambda item, enabled: (AtsRef("lever", "hooli"), "Hooli", "slug", True))
    db.query(AtsProbe).delete()
    db.commit()
    ats_resolver.resolve(SessionLocal, [{"company": "Hooli"}])
    listing = client.get(f"{API}/ats/following", headers=headers).json()["companies"]
    assert listing[0]["status"] == "following" and listing[0]["platform"] == "lever"


def test_remove_a_follow_still_being_looked_for(client, make_user):
    _, headers = make_user()
    body = client.post(f"{API}/ats/following/by-name", json={"name": "Vandelay"}, headers=headers).json()
    assert client.delete(f"{API}/ats/following/{body['follow']['id']}", headers=headers).status_code == 204
    assert client.get(f"{API}/ats/following", headers=headers).json()["companies"] == []


# ---------------------------------------------------------------- settings, suggestions, flags

def test_settings_defaults_and_validation(client, make_user, db):
    user_id, headers = make_user()
    titles_for(db, user_id, ["Data Engineer"])
    s = client.get(f"{API}/ats/following/settings", headers=headers).json()
    assert s["alert_mode"] == "daily" and s["default_titles"] == ["Data Engineer"]
    assert client.put(f"{API}/ats/following/settings", json={"alert_mode": "hourly"},
                      headers=headers).status_code == 422
    s = client.put(f"{API}/ats/following/settings", json={"alert_mode": "instant", "titles": ["SRE"]},
                   headers=headers).json()
    assert s["alert_mode"] == "instant" and s["titles"] == ["SRE"]


def test_suggestions_leave_out_followed_companies(client, make_user, db):
    user_id, headers = make_user()
    titles_for(db, user_id, ["Python Developer"])
    a, b = company(db, "acme"), company(db, "globex")
    add_jobs(db, a, [("Python Developer", "Bengaluru")])
    add_jobs(db, b, [("Python Developer", "Pune"), ("Python Engineer", "Pune")])
    follow_api(client, headers, a.id)
    names = [c["name"] for c in client.get(f"{API}/ats/suggestions", headers=headers).json()["companies"]]
    assert names == ["Globex"]


def test_search_groups_say_which_companies_are_followed(client, make_user, db):
    _, headers = make_user()
    a = company(db, "acme")
    add_jobs(db, a, [("Python Developer", "Bengaluru")])
    follow_api(client, headers, a.id)
    groups = client.get(f"{API}/ats/search", params={"q": "python developer"},
                        headers=headers).json()["groups"]
    assert groups and groups[0]["followed"] is True


def test_jobs_from_followed_companies_get_a_star_in_the_feed(client, make_user, db, monkeypatch):
    user_id, headers = make_user()
    follow_api(client, headers, company(db, "acme", "Acme Corp").id)
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [
        {"source": "LinkedIn", "title": "Python Dev", "company": "Acme Corp Pvt Ltd", "location": "Pune",
         "url": "https://www.linkedin.com/jobs/view/42", "description": "python " * 10},
        {"source": "LinkedIn", "title": "Python Dev", "company": "Hooli", "location": "Pune",
         "url": "https://www.linkedin.com/jobs/view/43", "description": "python " * 10}])
    monkeypatch.setattr(celery_tasks, "_score_batch_llm",
                        lambda jobs, **kw: [{**j, "llm_score": 90, "llm_reason": "fit"} for j in jobs])
    params = {"user_id": user_id, "resume_summary": "x", "skills": ["python"], "career_targets": [],
              "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 70}
    run_id = pipeline_progress.create_run(user_id=user_id, total_keywords=1)
    celery_tasks.search_and_score_keyword(run_id, "Python", ["Pune"], ["linkedin"], {}, params)
    feed = {j["company"]: j for j in pipeline_progress.current_jobs(run_id=run_id)}
    assert feed["Acme Corp Pvt Ltd"]["followed"] is True and feed["Hooli"]["followed"] is False


def test_old_hits_are_purged(make_user, db):
    user_id, _ = make_user()
    row = company(db, "acme")
    _hit(db, user_id, row)
    db.query(AtsFollowHit).update({AtsFollowHit.created_at: _now() - timedelta(days=40)})
    db.commit()
    assert target_companies.purge(db) == 1
