"""Fast apply in the user's own browser (P8b E3a): the run hands off to the extension, which asks for each
page's answers, saves the user's answers, fetches the resume and reports LinkedIn's confirmation."""
from __future__ import annotations

import json

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.apply import AnswerBankEntry, ApplyRun
from app.services import ai_router, extension_assist
from tests.test_apply_engine import API, _events, _job, _ready_user
from tests.test_extension import _pair

LI = "https://www.linkedin.com/jobs/view/backend-engineer-at-orbit-labs-4012345678"
PAGE = [
    {"key": "first", "label": "First name", "type": "text", "required": True},
    {"key": "phone", "label": "Mobile phone number", "type": "text", "required": True},
    {"key": "resume", "label": "Upload resume", "type": "file", "required": True},
    {"key": "auth", "label": "Are you legally authorized to work in India?", "type": "radio",
     "options": ["Yes", "No"], "required": True},
    {"key": "k8s", "label": "How many years of work experience do you have with Kubernetes?", "type": "text",
     "required": True},
    {"key": "gender", "label": "Gender", "type": "select", "options": ["Male", "Female", "Prefer not to say"],
     "required": False},
]


def _ai_unknown(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def complete(db, **kw):
        calls.append(kw)
        kw["validate"](json.dumps({"answers": [{"id": "k8s", "answer": "UNKNOWN"}]}))
        return type("R", (), {"engine": "test"})()

    monkeypatch.setattr(ai_router, "complete", complete)
    return calls


def _setup(client, make_user, *, accept_notice: bool = True):
    user_id, headers = _ready_user(client, make_user, level2=False)
    ext = {"Authorization": f"Bearer {_pair(client, headers)}"}
    if accept_notice:
        assert client.put(f"{API}/apply/settings", json={"accept_fast_notice": True},
                          headers=headers).status_code == 200
    return user_id, headers, ext


def test_linkedin_job_id_from_page_urls():
    assert extension_assist.linkedin_job_id(LI) == "4012345678"
    assert extension_assist.linkedin_job_id("https://in.linkedin.com/jobs/view/4012345678/?refId=x") == "4012345678"
    assert extension_assist.linkedin_job_id(
        "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=python") == "4012345678"
    assert extension_assist.linkedin_job_id("https://evil.example/jobs/view/4012345678") is None
    assert extension_assist.linkedin_job_id("https://www.linkedin.com/feed/") is None


def test_fast_apply_needs_the_extension_and_the_notice(client, make_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production")
    _, headers = _ready_user(client, make_user, level2=False)
    job = _job("Ext Need", apply_url=None, url=LI)
    r = client.post(f"{API}/apply/runs", json={"job_id": job, "mode": "fast"}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "extension_required"

    _pair(client, headers)
    r = client.post(f"{API}/apply/runs", json={"job_id": job, "mode": "fast"}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "fast_notice_required"
    settings = client.put(f"{API}/apply/settings", json={"accept_fast_notice": True}, headers=headers).json()
    assert settings["extension_connected"] and settings["fast_notice_accepted_at"] and "Submit" in settings["fast_notice"]

    r = client.post(f"{API}/apply/runs", json={"job_id": job, "mode": "fast"}, headers=headers)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["state"] == "in_browser" and run["runner"] == "extension" and "LinkedIn tab" in run["message"]
    # One open tab per job; turning the switch off cancels it.
    again = client.post(f"{API}/apply/runs", json={"job_id": job, "mode": "fast"}, headers=headers)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "run_active"
    client.put(f"{API}/apply/settings", json={"extension_assist": False}, headers=headers)
    assert client.get(f"{API}/apply/runs/{run['id']}", headers=headers).json()["state"] == "cancelled"
    r = client.post(f"{API}/apply/runs", json={"job_id": job, "mode": "fast"}, headers=headers)
    assert r.json()["detail"]["code"] == "extension_required"  # switched off = like no extension


def test_extension_fills_a_page_remembers_answers_and_reports_the_result(client, make_user, monkeypatch):
    ai_calls = _ai_unknown(monkeypatch)
    user_id, headers, ext = _setup(client, make_user)
    job = _job("Ext Fill", apply_url=None, url=LI)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": job, "mode": "fast"}, headers=headers).json()["id"]

    found = client.get(f"{API}/extension/runs/for-page",
                       params={"url": "https://www.linkedin.com/jobs/search/?currentJobId=4012345678"}, headers=ext)
    assert found.status_code == 200 and found.json()["run_id"] == run_id and found.json()["company"] == "Ext Fill"
    other = client.get(f"{API}/extension/runs/for-page",
                       params={"url": "https://www.linkedin.com/jobs/view/4099999999"}, headers=ext)
    assert other.json()["run_id"] is None

    r = client.post(f"{API}/extension/runs/{run_id}/fill", json={"fields": PAGE}, headers=ext)
    assert r.status_code == 200, r.text
    answers = {a["key"]: a for a in r.json()["answers"]}
    assert answers["first"]["value"] == "Jane" and answers["first"]["source"] == "profile"
    assert answers["auth"]["value"] == "Yes"
    assert answers["gender"]["value"] == "Female" and answers["gender"]["source"] == "eeo"
    assert answers["k8s"]["needs_input"] and not answers["k8s"]["value"]
    assert r.json()["files"] == {"resume": "resume"} and "resume" not in answers
    assert all("Female" not in json.dumps(c.get("messages", "")) for c in ai_calls)  # EEO never sent to the AI

    r = client.post(f"{API}/extension/runs/{run_id}/answer", json={"key": "k8s", "value": "3"}, headers=ext)
    assert r.status_code == 200 and r.json()["source"] == "user"
    bad = client.post(f"{API}/extension/runs/{run_id}/answer", json={"key": "auth", "value": "Maybe"}, headers=ext)
    assert bad.status_code == 422
    db = SessionLocal()
    try:
        mine = db.query(AnswerBankEntry).filter(AnswerBankEntry.user_id == user_id)
        entry = mine.filter(AnswerBankEntry.question.like("%Kubernetes%")).one()
        assert entry.answer == "3" and entry.approved
        assert mine.filter(AnswerBankEntry.question == "Gender").count() == 0
        plan = db.get(ApplyRun, run_id).plan
        gender = next(p for p in plan["fields"] if p["key"] == "gender")
        assert gender["value"] is None and gender["sealed"]  # EEO sealed at rest
    finally:
        db.close()

    # The same page read again (user went Back): answers kept, no new AI call.
    before = len(ai_calls)
    again = client.post(f"{API}/extension/runs/{run_id}/fill", json={"fields": PAGE}, headers=ext).json()
    assert {a["key"]: a["value"] for a in again["answers"]}["k8s"] == "3" and len(ai_calls) == before

    resume = client.get(f"{API}/extension/runs/{run_id}/resume", headers=ext)
    assert resume.status_code == 200 and resume.content[:2] == b"PK" and "cv.docx" in resume.headers[
        "content-disposition"]

    r = client.post(f"{API}/extension/runs/{run_id}/result", json={"outcome": "submitted"}, headers=ext)
    assert r.status_code == 200 and r.json()["state"] == "done"
    run = client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()
    assert run["submitted_at"] and ("submitted", "extension") in _events(run["application_id"])
    late = client.post(f"{API}/extension/runs/{run_id}/fill", json={"fields": PAGE}, headers=ext)
    assert late.status_code == 409 and late.json()["detail"]["code"] == "run_not_in_browser"


def test_closed_tab_asks_the_user_and_other_users_runs_stay_private(client, make_user, monkeypatch):
    _ai_unknown(monkeypatch)
    _, headers, ext = _setup(client, make_user)
    _, _, other_ext = _setup(client, make_user)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Ext Close", apply_url=None, url=LI),
                                                     "mode": "fast"}, headers=headers).json()["id"]
    assert client.post(f"{API}/extension/runs/{run_id}/fill", json={"fields": PAGE},
                       headers=other_ext).status_code == 404
    assert client.get(f"{API}/extension/runs/for-page", params={"url": LI}, headers=other_ext).json()["run_id"] is None
    assert client.get(f"{API}/extension/runs/{run_id}/resume", headers={"Authorization": "Bearer nope"}).status_code == 401

    r = client.post(f"{API}/extension/runs/{run_id}/result", json={"outcome": "closed"}, headers=ext)
    assert r.json()["state"] == "needs_user"
    run = client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()
    assert "Did you submit" in run["message"]
    done = client.post(f"{API}/apply/runs/{run_id}/done", json={"submitted": True}, headers=headers)
    assert done.status_code == 200 and done.json()["state"] == "done"


def test_assist_switch_off_blocks_the_extension_routes(client, make_user, monkeypatch):
    _ai_unknown(monkeypatch)
    _, headers, ext = _setup(client, make_user)
    client.put(f"{API}/apply/settings", json={"extension_assist": False}, headers=headers)
    r = client.get(f"{API}/extension/runs/for-page", params={"url": LI}, headers=ext)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "assist_off"
    assert client.get(f"{API}/extension/me", headers=ext).status_code == 200  # saving jobs still works


# ---------------------------------------------------------------- E3b: "Fill this form" anywhere

def _stopped_auto_run(user_id: str, job_id: str, *, url: str = "https://careers.acme.com/jobs/7") -> str:
    """An Auto apply run that stopped at a sign-in page, with one answer already prepared."""
    from app.models.apply import ApplyRun as Run
    db = SessionLocal()
    try:
        run = Run(user_id=user_id, job_id=job_id, job_key=f"acme|engineer|pune|{user_id}", company_title="acme|engineer",
                  mode="auto", level=2, adapter="generic", apply_url=url, state="needs_user",
                  message="The application page asks you to sign in or create an account.",
                  plan={"fields": [{"key": "notice", "label": "Notice period", "type": "text", "options": [],
                                    "required": True, "value": "30 days", "source": "user", "approved": True,
                                    "needs_input": False}]})
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def test_a_stopped_auto_run_continues_in_the_browser_with_its_answers(client, make_user, monkeypatch):
    ai_calls = _ai_unknown(monkeypatch)
    user_id, headers = _ready_user(client, make_user, level2=False)
    run_id = _stopped_auto_run(user_id, _job("Ext Open", apply_url="https://careers.acme.com/jobs/7"))
    r = client.post(f"{API}/apply/runs/{run_id}/browser", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "extension_required"

    ext = {"Authorization": f"Bearer {_pair(client, headers)}"}
    r = client.post(f"{API}/apply/runs/{run_id}/browser", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "in_browser" and r.json()["runner"] == "extension" and "Fill this form" in r.json()["message"]
    # Page 2 of the same company's form (another path) still finds the run; another site doesn't.
    found = client.get(f"{API}/extension/runs/for-page", params={"url": "https://careers.acme.com/apply/step-2"},
                       headers=ext).json()
    assert found["run_id"] == run_id
    assert client.get(f"{API}/extension/runs/for-page", params={"url": "https://other.example/apply"},
                      headers=ext).json()["run_id"] is None
    before = len(ai_calls)
    fill = client.post(f"{API}/extension/runs/{run_id}/fill", headers=ext, json={"fields": [
        {"key": "notice", "label": "Notice period", "type": "text", "required": True}]}).json()
    assert fill["answers"][0]["value"] == "30 days" and len(ai_calls) == before  # the run's answer, no AI call
    assert client.post(f"{API}/apply/runs/{run_id}/browser", headers=headers).status_code == 409  # already open

    r = client.post(f"{API}/extension/runs/{run_id}/result", json={"outcome": "closed"}, headers=ext)
    assert r.json()["state"] == "needs_user"
    assert "The tab was closed" in client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["message"]


def test_fill_this_form_on_a_page_with_no_run(client, make_user, monkeypatch):
    ai_calls = _ai_unknown(monkeypatch)
    user_id, headers, ext = _setup(client, make_user)
    fields = [{"key": "fn", "label": "First Name", "type": "text", "required": True},
              {"key": "cv", "label": "Resume/CV", "type": "file", "required": True},
              {"key": "gender", "label": "Gender", "type": "select", "options": ["Male", "Female"], "required": False},
              {"key": "k8s", "label": "How many years of work experience do you have with Kubernetes?",
               "type": "text", "required": True}]
    r = client.post(f"{API}/extension/page/fill", json={"fields": fields}, headers=ext)
    assert r.status_code == 200, r.text
    answers = {a["key"]: a for a in r.json()["answers"]}
    assert answers["fn"]["value"] == "Jane" and answers["gender"]["value"] == "Female"
    assert answers["k8s"]["needs_input"] and r.json()["files"] == {"cv": "resume"}
    assert all("Female" not in json.dumps(c.get("messages", "")) for c in ai_calls)

    client.post(f"{API}/extension/page/answer", headers=ext,
                json={"label": "How many years of work experience do you have with Kubernetes?", "value": "2"})
    client.post(f"{API}/extension/page/answer", headers=ext, json={"label": "Gender", "value": "Female"})
    db = SessionLocal()
    try:
        mine = db.query(AnswerBankEntry).filter(AnswerBankEntry.user_id == user_id)
        assert mine.filter(AnswerBankEntry.question.like("%Kubernetes%")).one().approved
        assert mine.filter(AnswerBankEntry.question == "Gender").count() == 0  # EEO
    finally:
        db.close()
    again = client.post(f"{API}/extension/page/fill", json={"fields": fields}, headers=ext).json()
    assert {a["key"]: a["value"] for a in again["answers"]}["k8s"] == "2"  # from the answer bank now

    resume = client.get(f"{API}/extension/page/resume", headers=ext)
    assert resume.status_code == 200 and resume.content[:2] == b"PK"
    client.put(f"{API}/apply/settings", json={"extension_assist": False}, headers=headers)
    assert client.post(f"{API}/extension/page/fill", json={"fields": fields}, headers=ext).status_code == 403
