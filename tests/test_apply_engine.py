"""P4b — apply engine: settings and risk notice, preflight (level, caps,
duplicates, company cooldown, switches), prepare -> answer -> review ->
approve -> submit with proof, hard stops, Fast apply, kill switches, proof
retention. The first part uses a fake adapter (no browser); the last part
runs the real engine + Chromium against local Greenhouse/Lever-style pages
(skipped when Chromium isn't installed)."""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.apply import adapters as adapter_registry
from app.apply.adapters import Adapter, HardStop
from app.apply.forms import Outcome
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.application import Application, ApplicationStatusEvent
from app.models.apply import AnswerBankEntry, ApplyProof, ApplyRun
from app.services import ai_router, apply_engine, entitlements, job_store

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "apply"
PNG = b"\x89PNG\r\n\x1a\n-fake-screenshot"

PARSED = {"name": "Jane Doe", "email": "jane@example.com", "headline": "Backend engineer",
          "summary": "Backend engineer building Python services.",
          "experience": [{"title": "Senior Engineer", "company": "Acme", "start": "Jan 2021", "current": True,
                          "highlights": ["Built FastAPI services on AWS"]}],
          "skills": ["Python", "FastAPI", "AWS"]}
PROFILE = {"applicant_info": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com",
                              "phone": "+91 90000 00000", "notice_period_days": 30, "requires_sponsorship": False},
           "work_authorization": [{"question": "Are you legally authorized to work in India?", "answer": "Yes"}],
           "eeo": {"gender": "Female"}}

FORM = [
    {"key": "first_name", "label": "First Name *", "type": "text", "options": [], "required": True},
    {"key": "email", "label": "Email *", "type": "text", "options": [], "required": True},
    {"key": "resume", "label": "Resume/CV *", "type": "file", "options": [], "required": True},
    {"key": "q_auth", "label": "Are you legally authorized to work in India? *", "type": "select",
     "options": ["Yes", "No"], "required": True},
    {"key": "q_why", "label": "Why do you want to join Orbit Labs? *", "type": "textarea", "options": [],
     "required": True},
    {"key": "gender", "label": "Gender", "type": "select", "options": ["Male", "Female", "Decline To Self Identify"],
     "required": False},
]


class FakeAdapter(Adapter):
    """Records what the engine asks for; no browser."""

    def __init__(self, name: str = "greenhouse", can_submit: bool = True):
        self.name, self.can_submit = name, can_submit
        self.fields = [dict(f) for f in FORM]
        self.open_error: str | None = None
        self.human: str | None = None
        self.result = Outcome("submitted", "Thank you for applying.")
        self.filled: list[tuple[dict, dict]] = []
        self.submits = 0

    def open(self, page, url, timeout_ms):
        if self.open_error:
            raise HardStop(self.open_error)

    def read_form(self, page):
        return [dict(f) for f in self.fields]

    def needs_human(self, page):
        return self.human

    def fill_form(self, page, fields, values, files):
        self.filled.append((dict(values), dict(files)))
        return []

    def submit_and_wait(self, page, wait_ms):
        self.submits += 1
        return self.result

    def screenshot(self, page):
        return PNG

    def page_url(self, page):
        return "https://job-boards.greenhouse.io/orbit/jobs/1"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """Fake adapters, no browser, no AI tailoring; documents are a local file."""
    fakes = {"greenhouse": FakeAdapter("greenhouse"), "lever": FakeAdapter("lever"),
             "linkedin": FakeAdapter("linkedin", can_submit=False)}
    monkeypatch.setattr(adapter_registry, "ADAPTERS", fakes)

    @contextlib.contextmanager
    def no_browser(**kw):
        yield object()

    monkeypatch.setattr(apply_engine, "browser_session", no_browser)
    resume = tmp_path / "Resume.docx"
    resume.write_bytes(b"PK-resume")

    def documents(db, run, workdir):
        return {"resume": str(resume)}

    monkeypatch.setattr(apply_engine, "_documents", documents)
    monkeypatch.setattr(apply_engine, "within_hours", lambda s: True)
    ai_calls: list[dict] = []

    def complete(db, **kw):  # the AI can't answer "Why us?" from the resume
        ai_calls.append(kw)
        kw["validate"](json.dumps({"answers": [{"id": "q_why", "answer": "UNKNOWN"}]}))
        return type("R", (), {"engine": "test"})()

    monkeypatch.setattr(ai_router, "complete", complete)
    return {"fakes": fakes, "ai_calls": ai_calls}


def _docx(text: str) -> bytes:
    import io
    from docx import Document
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _ready_user(client, make_user, *, level2: bool = True) -> tuple[str, dict]:
    user_id, headers = make_user()
    r = client.post(f"{API}/resumes", headers=headers, data={"parse": "false"},
                    files={"file": ("cv.docx", _docx(f"Jane Doe {user_id}"), "application/octet-stream")})
    client.put(f"{API}/resumes/{r.json()['id']}", json={"parsed": PARSED}, headers=headers)
    client.put(f"{API}/profile", json=PROFILE, headers=headers)
    if level2:
        db = SessionLocal()
        try:
            entitlements.set_override(db, user_id, "auto_apply", enabled=True, quota_per_day=None, max_level=2,
                                      expires_at=None)
            db.commit()
        finally:
            db.close()
        r = client.put(f"{API}/apply/settings", json={"accept_risk_notice": True, "level": 2}, headers=headers)
        assert r.status_code == 200, r.text
    return user_id, headers


def _job(company: str, *, title: str = "Backend Engineer", city: str = "Pune",
         apply_url: str | None = "https://job-boards.greenhouse.io/orbit/jobs/1",
         url: str = "https://www.linkedin.com/jobs/view/1") -> str:
    db = SessionLocal()
    try:
        job = {"source": "LinkedIn", "title": title, "company": company, "location": city,
               "url": f"{url}?c={company}&t={title}&l={city}", "description": "Python role.", "apply_url": apply_url}
        return job_store.upsert_copy(db, job, f"{company.lower()}|{title.lower()}|{city.lower()}").id
    finally:
        db.close()


def _step(step: str, run_id: str) -> ApplyRun:
    db = SessionLocal()
    try:
        getattr(apply_engine, step)(db, run_id)
        run = db.get(ApplyRun, run_id)
        db.expunge(run)
        return run
    finally:
        db.close()


def _events(application_id: str) -> list[tuple[str, str]]:
    db = SessionLocal()
    try:
        return [(e.status, e.status_source) for e in db.query(ApplicationStatusEvent)
                .filter_by(application_id=application_id).order_by(ApplicationStatusEvent.created_at)]
    finally:
        db.close()


# ---------------------------------------------------------------- settings

def test_settings_level_needs_entitlement_and_risk_notice(client, make_user):
    user_id, headers = _ready_user(client, make_user, level2=False)
    s = client.get(f"{API}/apply/settings", headers=headers).json()
    assert s["level"] == 1 and s["max_level"] == 1 and s["risk_accepted_at"] is None and "never submitted" in s["risk_notice"]
    assert s["caps"]["auto_per_day"] == 20 and s["usage_today"]["auto_submitted"] == 0

    r = client.put(f"{API}/apply/settings", json={"level": 2}, headers=headers)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "level_not_allowed"
    db = SessionLocal()
    try:
        entitlements.set_override(db, user_id, "auto_apply", enabled=True, quota_per_day=None, max_level=2,
                                  expires_at=None)
        db.commit()
    finally:
        db.close()
    r = client.put(f"{API}/apply/settings", json={"level": 2}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "risk_notice_required"
    ok = client.put(f"{API}/apply/settings", json={"level": 2, "accept_risk_notice": True,
                                                   "hours_start": 9, "hours_end": 18, "timezone": "Europe/Berlin"},
                    headers=headers).json()
    assert ok["level"] == 2 and ok["risk_accepted_at"] and (ok["hours_start"], ok["timezone"]) == (9, "Europe/Berlin")
    assert client.put(f"{API}/apply/settings", json={"hours_start": 20}, headers=headers).status_code == 422
    assert client.put(f"{API}/apply/settings", json={"timezone": "Mars/Base"}, headers=headers).status_code == 422


# ---------------------------------------------------------------- the level-2 flow

def test_auto_run_prepare_answer_approve_submit_with_proof(client, make_user, engine, dispatched):
    user_id, headers = _ready_user(client, make_user)
    job_id = _job("Orbit Labs")

    r = client.post(f"{API}/apply/runs", json={"job_id": job_id, "mode": "auto"}, headers=headers)
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["state"] == "queued" and run["adapter"] == "greenhouse" and run["level"] == 2
    assert run["apply_url"] == "https://job-boards.greenhouse.io/orbit/jobs/1"
    assert any(c["task"] == "job_finder.apply_prepare" and c["args"] == [run["id"]] for c in dispatched)
    again = client.post(f"{API}/apply/runs", json={"job_id": job_id, "mode": "auto"}, headers=headers)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "run_active"

    assert _step("prepare", run["id"]).state == "awaiting_input"
    got = client.get(f"{API}/apply/runs/{run['id']}", headers=headers).json()
    fields = {f["key"]: f for f in got["fields"]}
    assert (fields["first_name"]["value"], fields["first_name"]["source"]) == ("Jane", "profile")
    assert fields["q_auth"]["value"] == "Yes" and fields["q_why"]["needs_input"]
    assert (fields["gender"]["value"], fields["gender"]["source"]) == ("Female", "eeo")
    assert got["files"] == {"resume": "resume"} and "1 required question" in got["message"]
    # EEO never reaches the AI, and is sealed in the stored plan.
    assert all("Gender" not in c["user_prompt"] for c in engine["ai_calls"])
    db = SessionLocal()
    try:
        stored = next(p for p in db.get(ApplyRun, run["id"]).plan["fields"] if p["key"] == "gender")
        assert stored["value"] is None and stored["sealed"] and "Female" not in stored["sealed"]
    finally:
        db.close()
    assert client.get(f"{API}/apply/runs?state=waiting", headers=headers).json()["total"] == 1

    bad = client.post(f"{API}/apply/runs/{run['id']}/answer", json={"answers": {"q_auth": "Maybe"}}, headers=headers)
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_option"
    assert client.post(f"{API}/apply/runs/{run['id']}/approve", headers=headers).status_code == 409  # not ready yet
    answered = client.post(f"{API}/apply/runs/{run['id']}/answer",
                           json={"answers": {"q_why": "I like your product."}}, headers=headers).json()
    assert answered["state"] == "awaiting_review"
    bank = client.get(f"{API}/answer-bank", headers=headers).json()["answers"]
    assert any(a["question"].startswith("Why do you want to join") and a["approved"] for a in bank)

    approved = client.post(f"{API}/apply/runs/{run['id']}/approve", headers=headers)
    assert approved.status_code == 200 and approved.json()["state"] == "submitting"
    submit_call = next(c for c in dispatched if c["task"] == "job_finder.apply_submit")
    assert submit_call["args"] == [run["id"]] and "countdown" in submit_call["options"]

    done = _step("submit", run["id"])
    assert done.state == "submitted" and done.submitted_at is not None and done.active_key is None
    values, files = engine["fakes"]["greenhouse"].filled[-1]
    assert values["q_why"] == "I like your product." and values["gender"] == "Female" and files == {
        "resume": str(Path(files["resume"]))}

    proofs = client.get(f"{API}/apply/runs/{run['id']}/proofs", headers=headers).json()
    assert [p["kind"] for p in proofs] == ["confirmation"] and proofs[0]["confirmation_text"] == "Thank you for applying."
    img = client.get(f"{API}/apply/runs/{run['id']}/proofs/{proofs[0]['id']}", headers=headers)
    assert img.status_code == 200 and img.content == PNG and img.headers["content-type"] == "image/png"
    db = SessionLocal()
    try:
        assert b"PNG" not in db.get(ApplyProof, proofs[0]["id"]).ciphertext.encode()
        app_row = db.query(Application).filter_by(user_id=user_id).one()
        assert (app_row.status, app_row.status_source) == ("submitted", "apply_engine")
        assert job_store.get_user_job(db, user_id, job_id).state == "applied"
    finally:
        db.close()
    assert [s for s, _ in _events(app_row.id)] == ["queued", "preparing", "awaiting_input", "awaiting_review",
                                                   "submitting", "submitted"]
    _, other = make_user()
    assert client.get(f"{API}/apply/runs/{run['id']}", headers=other).status_code == 404
    assert client.get(f"{API}/apply/runs/{run['id']}/proofs/{proofs[0]['id']}", headers=other).status_code == 404

    # Never twice: same job, and the same role at the same company elsewhere.
    r = client.post(f"{API}/apply/runs", json={"job_id": job_id}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "already_applied"
    r = client.post(f"{API}/apply/runs", json={"job_id": _job("Orbit Labs", city="Mumbai")}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "company_cooldown"


def test_ai_drafts_used_in_a_run_are_approved_with_it(client, make_user, engine, monkeypatch):
    user_id, headers = _ready_user(client, make_user)

    def complete(db, **kw):
        kw["validate"](json.dumps({"answers": [{"id": "q_why", "answer": "I build Python services."}]}))
        return type("R", (), {"engine": "test"})()

    monkeypatch.setattr(ai_router, "complete", complete)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Draft Co")}, headers=headers).json()["id"]
    assert _step("prepare", run_id).state == "awaiting_review"
    got = client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()
    assert "drafted by AI" in got["message"]
    assert client.get(f"{API}/answer-bank?approved=false", headers=headers).json()["total"] == 1
    client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    assert client.get(f"{API}/answer-bank?approved=false", headers=headers).json()["total"] == 0


def test_preflight_refusals(client, make_user, engine, monkeypatch):
    _, low = _ready_user(client, make_user, level2=False)
    gh = _job("Pre Co")
    r = client.post(f"{API}/apply/runs", json={"job_id": gh}, headers=low)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "level_not_allowed"

    user_id, headers = _ready_user(client, make_user)
    client.put(f"{API}/apply/settings", json={"level": 1}, headers=headers)
    assert client.post(f"{API}/apply/runs", json={"job_id": gh}, headers=headers).json()["detail"]["code"] == "level_off"
    client.put(f"{API}/apply/settings", json={"level": 2}, headers=headers)

    easy = _job("Easy Co", apply_url=None)  # LinkedIn Easy Apply: never auto
    r = client.post(f"{API}/apply/runs", json={"job_id": easy}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "auto_not_supported"
    unknown = _job("Site Co", apply_url="https://careers.site.example/1")
    assert client.post(f"{API}/apply/runs", json={"job_id": unknown}, headers=headers).status_code == 409

    monkeypatch.setattr(get_settings(), "APPLY_DAILY_CAP", 0)
    r = client.post(f"{API}/apply/runs", json={"job_id": gh}, headers=headers)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "daily_cap"
    monkeypatch.setattr(get_settings(), "APPLY_DAILY_CAP", 20)

    client.put(f"{API}/apply/settings", json={"paused": True}, headers=headers)
    assert client.post(f"{API}/apply/runs", json={"job_id": gh}, headers=headers).json()["detail"]["code"] == "apply_paused"
    client.put(f"{API}/apply/settings", json={"paused": False}, headers=headers)

    _, no_resume = make_user()
    db = SessionLocal()
    try:
        entitlements.set_override(db, _, "auto_apply", enabled=True, quota_per_day=None, max_level=2, expires_at=None)
        db.commit()
    finally:
        db.close()
    client.put(f"{API}/apply/settings", json={"accept_risk_notice": True, "level": 2}, headers=no_resume)
    r = client.post(f"{API}/apply/runs", json={"job_id": gh}, headers=no_resume)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "resume_required"


def test_platform_cap_counts_submitted_runs(client, make_user, engine, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    first = client.post(f"{API}/apply/runs", json={"job_id": _job("Cap One")}, headers=headers).json()["id"]
    _step("prepare", first)
    client.post(f"{API}/apply/runs/{first}/answer", json={"answers": {"q_why": "Product."}}, headers=headers)
    client.post(f"{API}/apply/runs/{first}/approve", headers=headers)
    assert _step("submit", first).state == "submitted"
    assert client.get(f"{API}/apply/settings", headers=headers).json()["usage_today"]["by_platform"] == {"greenhouse": 1}
    monkeypatch.setattr(get_settings(), "APPLY_PLATFORM_DAILY_CAP", 1)
    r = client.post(f"{API}/apply/runs", json={"job_id": _job("Cap Two")}, headers=headers)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "platform_cap"
    lever = _job("Cap Three", apply_url="https://jobs.lever.co/capthree/1")
    assert client.post(f"{API}/apply/runs", json={"job_id": lever}, headers=headers).status_code == 202


def test_outside_hours_approval_waits(client, make_user, engine, monkeypatch):
    _, headers = _ready_user(client, make_user)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Night Co")}, headers=headers).json()["id"]
    _step("prepare", run_id)
    client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"q_why": "Product."}}, headers=headers)
    monkeypatch.setattr(apply_engine, "within_hours", lambda s: False)
    r = client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "outside_hours"
    assert client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["state"] == "awaiting_review"


# ---------------------------------------------------------------- hard stops

def test_captcha_and_login_hand_the_run_to_the_user(client, make_user, engine):
    user_id, headers = _ready_user(client, make_user)
    gh = engine["fakes"]["greenhouse"]

    gh.open_error = "The application page asks you to sign in."
    login = client.post(f"{API}/apply/runs", json={"job_id": _job("Login Co")}, headers=headers).json()["id"]
    run = _step("prepare", login)
    assert run.state == "needs_user" and "sign in" in run.message
    gh.open_error = None

    cap = client.post(f"{API}/apply/runs", json={"job_id": _job("Captcha Co")}, headers=headers).json()["id"]
    _step("prepare", cap)
    client.post(f"{API}/apply/runs/{cap}/answer", json={"answers": {"q_why": "Product."}}, headers=headers)
    client.post(f"{API}/apply/runs/{cap}/approve", headers=headers)
    gh.result = Outcome("captcha", "A captcha appeared after submitting.")
    run = _step("submit", cap)
    assert run.state == "needs_user" and "captcha" in run.message and gh.submits == 1
    assert [p["kind"] for p in client.get(f"{API}/apply/runs/{cap}/proofs", headers=headers).json()] == ["stopped"]

    done = client.post(f"{API}/apply/runs/{cap}/done", json={"submitted": True}, headers=headers).json()
    assert done["state"] == "done"
    db = SessionLocal()
    try:
        row = db.query(Application).filter(Application.id == done["application_id"]).one()
        assert (row.status, row.status_source) == ("submitted", "manual")
    finally:
        db.close()


def test_human_checkbox_form_is_prepared_but_handed_over(client, make_user, engine):
    _, headers = _ready_user(client, make_user)
    engine["fakes"]["greenhouse"].human = "This form has an \"I am human\" captcha, so it can't be submitted for you."
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Human Co")}, headers=headers).json()["id"]
    run = _step("prepare", run_id)
    assert run.state == "needs_user" and "I am human" in run.message and "copy them" in run.message
    fields = {f["key"]: f for f in client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["fields"]}
    assert fields["first_name"]["value"] == "Jane"  # the answers are still there to copy
    assert engine["fakes"]["greenhouse"].submits == 0


def test_form_changed_since_review_is_not_submitted(client, make_user, engine):
    _, headers = _ready_user(client, make_user)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Changed Co")}, headers=headers).json()["id"]
    _step("prepare", run_id)
    client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"q_why": "Product."}}, headers=headers)
    client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    gh = engine["fakes"]["greenhouse"]
    gh.fields.append({"key": "q_new", "label": "Salary expectation? *", "type": "text", "options": [], "required": True})
    run = _step("submit", run_id)
    assert run.state == "needs_user" and "form changed" in run.message and gh.submits == 0


def test_required_file_we_cannot_provide_stops(client, make_user, engine):
    _, headers = _ready_user(client, make_user)
    engine["fakes"]["greenhouse"].fields.append(
        {"key": "portfolio_pdf", "label": "Portfolio (PDF) *", "type": "file", "options": [], "required": True})
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("File Co")}, headers=headers).json()["id"]
    run = _step("prepare", run_id)
    assert run.state == "needs_user" and "Portfolio" in run.message


# ---------------------------------------------------------------- fast apply

def test_fast_apply_fills_linkedin_and_never_submits(client, make_user, engine, monkeypatch):
    _, headers = _ready_user(client, make_user, level2=False)
    easy = _job("Fast Co", apply_url=None)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": easy, "mode": "fast"}, headers=headers).json()["id"]
    run = _step("prepare", run_id)
    li = engine["fakes"]["linkedin"]
    assert run.state == "needs_user" and "click Submit yourself" in run.message
    assert li.filled and li.submits == 0
    assert client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers).status_code == 409

    r = client.post(f"{API}/apply/runs", json={"job_id": _job("Fast Two"), "mode": "fast"}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "fast_not_supported"  # a Greenhouse job
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production")
    r = client.post(f"{API}/apply/runs", json={"job_id": _job("Fast Three", apply_url=None), "mode": "fast"},
                    headers=headers)
    assert r.status_code == 409 and "local apply agent" in r.json()["detail"]["message"]


# ---------------------------------------------------------------- switches

def test_kill_switch_pause_and_disable_cancel_runs(client, make_user, make_admin, engine):
    user_id, headers = _ready_user(client, make_user)
    _, admin = make_admin()
    a = client.post(f"{API}/apply/runs", json={"job_id": _job("Kill A")}, headers=headers).json()["id"]
    assert client.put(f"{API}/admin/flags/auto_apply_enabled", json={"value": False}, headers=admin).status_code == 200
    assert client.get(f"{API}/apply/runs/{a}", headers=headers).json()["state"] == "cancelled"
    assert client.post(f"{API}/apply/runs", json={"job_id": _job("Kill B")}, headers=headers).status_code == 503
    client.put(f"{API}/admin/flags/auto_apply_enabled", json={"value": True}, headers=admin)

    b = client.post(f"{API}/apply/runs", json={"job_id": _job("Kill C")}, headers=headers).json()["id"]
    client.put(f"{API}/apply/settings", json={"paused": True}, headers=headers)
    assert client.get(f"{API}/apply/runs/{b}", headers=headers).json()["state"] == "cancelled"
    client.put(f"{API}/apply/settings", json={"paused": False}, headers=headers)

    c = client.post(f"{API}/apply/runs", json={"job_id": _job("Kill D")}, headers=headers).json()["id"]
    assert client.post(f"{API}/admin/users/{user_id}/disable", headers=admin).status_code == 200
    db = SessionLocal()
    try:
        assert db.get(ApplyRun, c).state == "cancelled"
    finally:
        db.close()
    assert _step("prepare", c).state == "cancelled"  # a late worker does nothing


def test_cancel_run(client, make_user, engine):
    _, headers = _ready_user(client, make_user)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Cancel Co")}, headers=headers).json()["id"]
    assert client.post(f"{API}/apply/runs/{run_id}/cancel", headers=headers).json()["state"] == "cancelled"
    assert client.post(f"{API}/apply/runs/{run_id}/cancel", headers=headers).status_code == 409
    # a cancelled run frees the job for a new one
    assert client.post(f"{API}/apply/runs", json={"job_id": _job("Cancel Co")}, headers=headers).status_code == 202


def test_old_proofs_are_purged(client, make_user, engine):
    user_id, headers = _ready_user(client, make_user)
    run_id = client.post(f"{API}/apply/runs", json={"job_id": _job("Purge Co")}, headers=headers).json()["id"]
    db = SessionLocal()
    try:
        run = db.get(ApplyRun, run_id)
        old = apply_engine.store_proof(db, run, PNG, kind="stopped", text=None, url=None)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=91)
        new = apply_engine.store_proof(db, run, PNG, kind="stopped", text=None, url=None)
        db.commit()
        old_id, new_id = old.id, new.id
        assert apply_engine.purge_old_proofs(db) >= 1
        assert db.get(ApplyProof, old_id) is None and db.get(ApplyProof, new_id) is not None
    finally:
        db.close()


# ---------------------------------------------------------------- real browser on local pages

def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


browser = pytest.mark.skipif(not _chromium_ok(), reason="Playwright Chromium is not installed")


@pytest.fixture
def real_browser_engine(monkeypatch, tmp_path):
    """Real adapters and browser; documents are local files; the AI says UNKNOWN."""
    resume = tmp_path / "Jane Doe Resume.docx"
    resume.write_bytes(_docx("Jane Doe"))
    letter = tmp_path / "Cover letter.txt"
    letter.write_text("Dear Hiring Team, ...", encoding="utf-8")
    monkeypatch.setattr(apply_engine, "_documents",
                        lambda db, run, workdir: {"resume": str(resume), "cover_letter": str(letter)})
    monkeypatch.setattr(apply_engine, "within_hours", lambda s: True)

    def complete(db, **kw):
        kw["validate"](json.dumps({"answers": []}))
        return type("R", (), {"engine": "test"})()

    monkeypatch.setattr(ai_router, "complete", complete)
    captured: dict = {}
    real_submit_and_wait = Adapter.submit_and_wait

    def spy(self, page, wait_ms):
        result = real_submit_and_wait(self, page, wait_ms)
        captured["form"] = page.evaluate("window.__submitted || null")
        return result

    monkeypatch.setattr(Adapter, "submit_and_wait", spy)
    return captured


def _local_run(user_id: str, adapter: str, url: str, company: str) -> str:
    job_id = _job(company, apply_url=None)
    db = SessionLocal()
    try:
        run = ApplyRun(user_id=user_id, job_id=job_id, job_key=f"{company.lower()}|backend engineer|pune",
                       company_title=f"{company.lower()}|backend engineer", mode="auto", level=2, adapter=adapter,
                       apply_url=url, state="queued", active_key=f"{user_id}|{company}", plan={})
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


@browser
def test_greenhouse_page_end_to_end(client, make_user, real_browser_engine):
    user_id, headers = _ready_user(client, make_user)
    url = (FIXTURES / "greenhouse_form.html").as_uri()
    run_id = _local_run(user_id, "greenhouse", url, "GH Local")
    # The company page embeds the Greenhouse form in an iframe (gh_jid pages).
    run = _step("prepare", run_id)
    assert run.state == "awaiting_input", run.message
    fields = {f["key"]: f for f in client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["fields"]}
    assert fields["first_name"]["value"] == "Jane" and fields["email"]["value"] == "jane@example.com"
    assert fields["question_2"]["value"] == "Yes" and fields["question_2"]["options"] == ["Yes", "No"]
    assert fields["question_3"]["type"] == "radio" and fields["question_3"]["value"] == "No"
    assert fields["gender"]["value"] == "Female" and fields["question_4"]["needs_input"]
    assert client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["files"] == {
        "resume": "resume", "cover_letter": "cover_letter"}

    client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"question_4": "Your product."}}, headers=headers)
    assert client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers).json()["state"] == "submitting"
    run = _step("submit", run_id)
    assert run.state == "submitted", run.message
    form = real_browser_engine["form"]
    assert form["first_name"] == "Jane" and form["question_2"] == "1" and form["question_3"] == "no"
    assert form["question_4"] == "Your product." and form["gender"] == "Female"
    assert form["resume"] == "Jane Doe Resume.docx" and form["cover_letter"] == "Cover letter.txt"
    proofs = client.get(f"{API}/apply/runs/{run_id}/proofs", headers=headers).json()
    assert proofs[0]["kind"] == "confirmation" and "Thank you for applying" in proofs[0]["confirmation_text"]
    png = client.get(f"{API}/apply/runs/{run_id}/proofs/{proofs[0]['id']}", headers=headers).content
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@browser
def test_greenhouse_embedded_iframe_and_captcha(client, make_user, real_browser_engine, tmp_path):
    user_id, headers = _ready_user(client, make_user)
    form_url = (FIXTURES / "greenhouse_form.html").as_uri() + "?captcha=1"
    company_page = tmp_path / "careers.html"
    company_page.write_text(f'<h1>Careers</h1><iframe id="grnhse_iframe" src="{form_url}"></iframe>', encoding="utf-8")
    run_id = _local_run(user_id, "greenhouse", company_page.as_uri() + "?gh_jid=1", "GH Captcha")
    assert _step("prepare", run_id).state == "awaiting_input"
    client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"question_4": "Your product."}}, headers=headers)
    client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    run = _step("submit", run_id)
    assert run.state == "needs_user" and "captcha" in run.message


@browser
def test_lever_page_end_to_end(client, make_user, real_browser_engine):
    user_id, headers = _ready_user(client, make_user)
    run_id = _local_run(user_id, "lever", (FIXTURES / "lever_form.html").as_uri(), "Lever Local")
    run = _step("prepare", run_id)
    assert run.state == "awaiting_review", run.message  # name, email, notice period and pronouns all answered
    fields = {f["key"]: f for f in client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["fields"]}
    assert fields["name"]["value"] == "Jane Doe" and fields["cards[abc][field0]"]["value"] == "30 days"
    assert fields["eeo[pronouns]"]["value"] == "Prefer not to say"
    client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    assert _step("submit", run_id).state == "submitted"
    form = real_browser_engine["form"]
    assert form["name"] == "Jane Doe" and form["email"] == "jane@example.com" and form["resume"] == "Jane Doe Resume.docx"
    assert form["cards[abc][field0]"] == "30 days" and form["comments"] == ""


@browser
def test_lever_human_checkbox_is_handed_over(client, make_user, real_browser_engine):
    user_id, headers = _ready_user(client, make_user)
    url = (FIXTURES / "lever_form.html").as_uri() + "?hcaptcha=1"
    run = _step("prepare", _local_run(user_id, "lever", url, "Lever Human"))
    assert run.state == "needs_user" and "I am human" in run.message


def test_runs_can_be_listed_for_one_job(client, make_user, engine):
    _, headers = _ready_user(client, make_user)
    a, b = _job("List A"), _job("List B")
    run_a = client.post(f"{API}/apply/runs", json={"job_id": a}, headers=headers).json()["id"]
    client.post(f"{API}/apply/runs", json={"job_id": b}, headers=headers)
    got = client.get(f"{API}/apply/runs", params={"job_id": a}, headers=headers).json()
    assert got["total"] == 1 and got["runs"][0]["id"] == run_a and got["runs"][0]["title"] == "Backend Engineer"
    _, other = make_user()
    assert client.get(f"{API}/apply/runs", params={"job_id": a}, headers=other).json()["total"] == 0
