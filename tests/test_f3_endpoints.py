"""Endpoints the F3 screens need: cancel a streaming run, and
"Apply manually → Mark as applied"."""
from __future__ import annotations

from app.core.db import SessionLocal
from app.models.jobs import UserJob
from app.services import job_dedupe, job_store, pipeline_progress

API = "/api/v1"


def test_cancel_pipeline_run_is_owner_only_and_stops_pending_keywords(client, make_user):
    user_id, headers = make_user()
    _, other = make_user()
    run_id = pipeline_progress.create_run(user_id=user_id, total_keywords=3)
    assert client.post(f"{API}/pipeline/{run_id}/cancel", headers=other).status_code == 404
    r = client.post(f"{API}/pipeline/{run_id}/cancel", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert pipeline_progress.is_cancelled(run_id)
    assert client.post(f"{API}/pipeline/{run_id}/cancel", headers=headers).json()["status"] == "cancelled"

    done = pipeline_progress.create_run(user_id=user_id, total_keywords=1)
    pipeline_progress.increment_keyword_completed(run_id=done)
    assert client.post(f"{API}/pipeline/{done}/cancel", headers=headers).json()["status"] == "completed"


def test_mark_applied_manually_records_application_and_job_state(client, make_user):
    user_id, headers = make_user()
    db = SessionLocal()
    try:
        job = {"source": "Indeed", "title": "Backend Engineer", "company": "Manual Co", "location": "Pune",
               "url": "https://indeed.example/manual-1", "description": "x"}
        row = job_store.upsert_copy(db, job, job_dedupe.job_key(job))
        job_id = row.id
    finally:
        db.close()

    assert client.post(f"{API}/applications/manual", json={"job_id": "nope"}, headers=headers).status_code == 404
    r = client.post(f"{API}/applications/manual", json={"job_id": job_id}, headers=headers)
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["status"] == "submitted" and entry["status_source"] == "manual" and entry["company"] == "Manual Co"

    db = SessionLocal()
    try:
        uj = db.query(UserJob).filter(UserJob.user_id == user_id, UserJob.job_id == job_id).one()
        assert uj.state == "applied"
    finally:
        db.close()
    applied = client.get(f"{API}/jobs?state=applied", headers=headers).json()
    assert [j["id"] for j in applied["jobs"]] == [job_id]
    # Marking twice keeps one application.
    client.post(f"{API}/applications/manual", json={"job_id": job_id}, headers=headers)
    apps = client.get(f"{API}/applications", headers=headers).json()
    assert sum(a["company"] == "Manual Co" for a in apps["applications"]) == 1
