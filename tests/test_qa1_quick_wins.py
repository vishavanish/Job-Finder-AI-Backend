"""QA1 quick wins: the latest search comes back after leaving Jobs (§4),
"will run" on Job sources matches what a search runs (§6), and a failed
prep pack has a short message with the detail kept apart (§7)."""
from __future__ import annotations

from app.services import pipeline_progress

API = "/api/v1"


# ---------------------------------------------------------------- §4 latest search

def test_latest_run_is_the_newest_search_of_this_user(client, make_user):
    user_id, headers = make_user()
    _, other = make_user()
    assert client.get(f"{API}/pipeline/runs/latest", headers=headers).status_code == 404

    pipeline_progress.create_run(user_id=user_id, total_keywords=1, keywords=["old"])
    newest = pipeline_progress.create_run(user_id=user_id, total_keywords=2, keywords=["AI Engineer", "SDE"],
                                          skipped_sources=["Adzuna: needs an Adzuna API key"])
    pipeline_progress.create_run(user_id=user_id, total_keywords=1, kind="alert")  # background: never "latest"

    r = client.get(f"{API}/pipeline/runs/latest", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == newest and body["status"] == "running"
    assert body["keywords"] == ["AI Engineer", "SDE"]
    assert body["skipped_sources"] == ["Adzuna: needs an Adzuna API key"]
    assert body["created_at"]
    assert client.get(f"{API}/pipeline/runs/latest", headers=other).status_code == 404


def test_latest_run_reports_completion(client, make_user):
    user_id, headers = make_user()
    run_id = pipeline_progress.create_run(user_id=user_id, total_keywords=1)
    pipeline_progress.increment_keyword_completed(run_id=run_id)
    assert client.get(f"{API}/pipeline/runs/latest", headers=headers).json()["status"] == "completed"


def test_latest_route_does_not_shadow_run_ids(client, make_user):
    user_id, headers = make_user()
    run_id = pipeline_progress.create_run(user_id=user_id, total_keywords=1)
    assert client.get(f"{API}/pipeline/{run_id}", headers=headers).json()["run_id"] == run_id


# ---------------------------------------------------------------- §6 "will run"

def test_providers_page_only_says_will_run_for_sources_a_search_runs(client, make_user):
    from app.core.db import SessionLocal
    from app.models.user import User
    from app.services import provider_catalog

    user_id, headers = make_user()
    view = {s["key"]: s for s in client.get(f"{API}/providers", headers=headers).json()["sources"]}
    assert view["naukri"]["available"] is False
    assert view["naukri"]["reason"] == "you paste these jobs in; searches don't fetch them"
    assert view["naukri_apify"]["available"] is False

    db = SessionLocal()
    try:
        plan = provider_catalog.effective_sources(db, db.get(User, user_id), requested=None,
                                                  want_naukri_apify=False)
    finally:
        db.close()
    shown = {k for k, s in view.items() if s["available"]}
    assert shown == set(plan.sources)  # the page's count is what a search really runs
