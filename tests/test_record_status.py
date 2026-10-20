from app.core.db import SessionLocal
from app.models.application import Application, ApplicationStatusEvent
from app.services.applications_store import record_status


def _rows(user_id):
    db = SessionLocal()
    try:
        return db.query(Application).filter(Application.user_id == user_id).all()
    finally:
        db.close()


def test_same_job_from_two_sources_is_one_application(make_user):
    user_id, _ = make_user()
    indeed = {"source": "Indeed", "title": "Backend Engineer", "company": "Acme Ltd",
              "location": "Pune", "url": "https://indeed.example/job/1"}
    linkedin = {"source": "LinkedIn", "title": "Backend Engineer", "company": "ACME",
                "location": "Pune, India", "url": "https://linkedin.example/job/9"}

    first = record_status(user_id=user_id, job=indeed, status="opened")
    second = record_status(user_id=user_id, job=linkedin, status="submitted", status_source="manual")

    assert first.id == second.id
    [row] = _rows(user_id)
    assert row.status == "submitted"
    assert row.job_url == indeed["url"]
    assert row.alias_urls == [linkedin["url"]]


def test_alias_not_duplicated_and_every_change_has_an_event(make_user):
    user_id, _ = make_user()
    job = {"title": "SRE", "company": "Globex", "location": "Mumbai", "url": "https://a.example/1"}
    other = {**job, "url": "https://b.example/1"}

    row = record_status(user_id=user_id, job=job, status="opened")
    record_status(user_id=user_id, job=other, status="opened")
    record_status(user_id=user_id, job=other, status="interview")

    [stored] = _rows(user_id)
    assert stored.alias_urls == ["https://b.example/1"]
    db = SessionLocal()
    try:
        events = db.query(ApplicationStatusEvent).filter_by(application_id=row.id).count()
    finally:
        db.close()
    assert events == 3


def test_different_users_never_share_rows(make_user):
    alice, _ = make_user()
    bob, _ = make_user()
    job = {"title": "SRE", "company": "Initech", "location": "Pune", "url": "https://c.example/1"}

    a = record_status(user_id=alice, job=job, status="opened")
    b = record_status(user_id=bob, job=job, status="opened")
    assert a.id != b.id
