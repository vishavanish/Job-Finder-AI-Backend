"""P5 — Tracker & Gmail: OAuth connect (single-use state, scope check, sealed
token), sync (idempotent, history fallback, revoked grant), prefilter, AI
classification, matching thresholds, status order, rejection note, needs-review
inbox, job-alert emails -> jobs -> the "Job alert emails" search source,
disconnect, retention, tracker stats and timeline. A fake Gmail client and a
fake AI; nothing leaves the machine."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.application import Application
from app.models.email import EmailAccount, EmailEvent, EmailJob
from app.providers import gmail
from app.services import ai_router, email_sync, gmail_accounts, job_sources, key_store
from app.services.applications_store import record_status

API = "/api/v1"
BODY_MARKER = "UNIQUE-BODY-TEXT-7731"  # appears only in bodies, which must never be stored

NAUKRI_ALERT = """<table>
<tr><td><a href="https://www.naukri.com/job-listings-python-developer-orbit-labs-pune-3-to-5-years-201024000001">Python Developer</a></td></tr>
<tr><td>Orbit Labs</td></tr><tr><td>3-5 Yrs</td></tr><tr><td>Pune</td></tr>
<tr><td><a href="https://click.naukri.com/r?url=https%3A%2F%2Fwww.naukri.com%2Fjob-listings-data-engineer-kite-mumbai-2-to-4-years-201024000002">Data Engineer</a></td></tr>
<tr><td>Kite Analytics</td></tr><tr><td>Mumbai</td></tr></table>"""


def _ts(days_ago: float = 0) -> str:
    return str(int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000))


class FakeGmail:
    """Messages keyed by id; search returns ids newest first like Gmail."""

    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.history_id = "100"
        self.history_new: list[str] | None = None
        self.history_expired = False
        self.revoked = False
        self.searches: list[str] = []
        self.body_calls: list[str] = []

    def add(self, mid, *, sender, subject, snippet="", text="", html="", thread="", days_ago=0.0):
        self.messages[mid] = {"id": mid, "threadId": thread or f"t-{mid}", "snippet": snippet or subject,
                              "internalDate": _ts(days_ago), "text": text, "html": html,
                              "payload": {"headers": [{"name": "From", "value": sender},
                                                      {"name": "Subject", "value": subject}]}}

    def profile(self):
        if self.revoked:
            raise gmail.GrantRevoked("revoked")
        return {"emailAddress": "jane@gmail.com", "historyId": self.history_id}

    def search(self, query, max_results):
        if self.revoked:
            raise gmail.GrantRevoked("revoked")
        self.searches.append(query)
        return sorted(self.messages, key=lambda m: self.messages[m]["internalDate"], reverse=True)[:max_results]

    def history(self, start, max_results):
        if self.history_expired:
            raise gmail.HistoryExpired()
        return list(self.history_new or []), str(int(self.history_id) + 1)

    def metadata(self, mid):
        return gmail.meta_from(self.messages[mid])

    def body(self, mid):
        self.body_calls.append(mid)
        m = self.messages[mid]
        return m["text"], m["html"]


class FakeAI:
    """Answers email_classify from a table keyed by a word in the subject."""

    def __init__(self):
        self.table: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.down = False

    def __call__(self, db, **kw):
        assert kw["task"] == "email_classify"
        self.calls.append(kw)
        if self.down:
            raise ai_router.NoModelAvailable("email_classify", ["test: quota"])
        subject = kw["user_prompt"].split("SUBJECT: ", 1)[1].split("\n", 1)[0].lower()
        reply = next((v for k, v in self.table.items() if k in subject),
                     {"category": "other", "confidence": 0.9, "company": "", "role": "", "summary": ""})
        assert kw["validate"](json.dumps(reply))
        return type("R", (), {"engine": "groq:test"})()


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr(ai_router, "complete", fake)
    return fake


@pytest.fixture
def gmail_configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "GMAIL_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(s, "GMAIL_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(s, "APP_PUBLIC_URL", "http://app.test")


def _account(user_id: str, address: str = "jane@gmail.com") -> str:
    db = SessionLocal()
    try:
        row = EmailAccount(user_id=user_id, gmail_address=address, token_sealed=key_store.seal("refresh-123"),
                           scopes=gmail.READONLY, status="active", consecutive_failures=0,
                           connected_at=datetime.now(timezone.utc))
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _applied(user_id: str, company: str, title: str, status: str = "submitted") -> str:
    return record_status(user_id=user_id, job={"url": f"https://jobs.example/{company}/{title}", "title": title,
                                               "company": company, "source": "LinkedIn"},
                         status=status, status_source="manual").id


def _sync(account_id: str, fake: FakeGmail) -> dict:
    db = SessionLocal()
    try:
        return email_sync.sync_account(db, account_id, client_factory=lambda acc: fake)
    finally:
        db.close()


def _app(app_id: str) -> Application:
    db = SessionLocal()
    try:
        row = db.get(Application, app_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _events(user_id: str) -> list[EmailEvent]:
    db = SessionLocal()
    try:
        rows = db.query(EmailEvent).filter_by(user_id=user_id).order_by(EmailEvent.received_at).all()
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()


REJECTION = {"category": "rejection", "confidence": 0.95, "company": "Acme Corp", "role": "Backend Engineer",
             "summary": "Acme Corp will not move forward with the application."}
INTERVIEW = {"category": "interview_invite", "confidence": 0.9, "company": "", "role": "",
             "summary": "Invitation to schedule a call."}


# ---------------------------------------------------------------- connect

def test_connect_callback_and_accounts(client, make_user, gmail_configured, monkeypatch):
    user_id, headers = make_user()
    r = client.get(f"{API}/gmail/connect", headers=headers)
    assert r.status_code == 200
    url = urlparse(r.json()["auth_url"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com" and gmail.READONLY in q["scope"][0]
    assert "gmail.send" not in q["scope"][0] and q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    state = q["state"][0]

    seen: dict = {}

    def exchange(code, verifier):
        seen["verifier"] = verifier
        return {"refresh_token": "refresh-xyz", "scope": f"{gmail.READONLY} openid email"}

    monkeypatch.setattr(gmail, "exchange_code", exchange)
    monkeypatch.setattr(gmail, "GmailClient", lambda token: type("C", (), {"profile": lambda self: {
        "emailAddress": "Jane@Gmail.com"}})())
    back = client.get(f"{API}/gmail/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "http://app.test/tracker?gmail=connected"
    assert len(seen["verifier"]) >= 43

    accounts = client.get(f"{API}/gmail/accounts", headers=headers).json()
    assert accounts["configured"] and accounts["accounts"][0]["gmail_address"] == "jane@gmail.com"
    db = SessionLocal()
    try:
        row = db.query(EmailAccount).filter_by(user_id=user_id).one()
        assert "refresh-xyz" not in row.token_sealed and key_store.unseal(row.token_sealed) == "refresh-xyz"
    finally:
        db.close()

    # the state is single use
    again = client.get(f"{API}/gmail/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
    assert "gmail=error" in again.headers["location"] and "reason=expired" in again.headers["location"]


def test_callback_refuses_missing_scope_and_denied(client, make_user, gmail_configured, monkeypatch):
    _, headers = make_user()
    monkeypatch.setattr(gmail, "exchange_code", lambda c, v: {"refresh_token": "r", "scope": "openid email"})
    state = parse_qs(urlparse(client.get(f"{API}/gmail/connect", headers=headers).json()["auth_url"]).query)["state"][0]
    r = client.get(f"{API}/gmail/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
    assert "reason=scope" in r.headers["location"]
    state = parse_qs(urlparse(client.get(f"{API}/gmail/connect", headers=headers).json()["auth_url"]).query)["state"][0]
    r = client.get(f"{API}/gmail/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert "reason=denied" in r.headers["location"]
    assert client.get(f"{API}/gmail/accounts", headers=headers).json()["accounts"] == []


def test_connect_needs_server_config_and_has_a_limit(client, make_user, gmail_configured, monkeypatch):
    user_id, headers = make_user()
    for i in range(3):
        _account(user_id, f"a{i}@gmail.com")
    r = client.get(f"{API}/gmail/connect", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "gmail_limit"
    _, other = make_user()
    monkeypatch.setattr(get_settings(), "GMAIL_CLIENT_ID", "")
    r = client.get(f"{API}/gmail/connect", headers=other)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "gmail_not_configured"


# ---------------------------------------------------------------- sync + classify + match

def test_rejection_updates_the_application_with_a_formal_note(client, make_user, ai):
    user_id, headers = make_user()
    acme = _applied(user_id, "Acme Corp", "Backend Engineer")
    beta = _applied(user_id, "Beta Labs", "ML Engineer")
    account = _account(user_id)
    fake = FakeGmail()
    fake.add("m1", sender="Acme Corp <no-reply@greenhouse.io>", subject="Your application to Acme Corp",
             text=f"Thank you for applying for the Backend Engineer role at Acme Corp. Unfortunately we will not "
                  f"move forward. {BODY_MARKER}", days_ago=1)
    fake.add("m2", sender="Shop <deals@shop.example>", subject="50% off shoes", text="Big sale")  # prefilter drops it
    ai.table["acme"] = REJECTION

    out = _sync(account, fake)
    assert out["applied"] == 1 and out["job_related"] == 1
    assert _app(acme).status == "rejected" and _app(acme).status_source == "email_parser"
    assert _app(beta).status == "submitted"
    assert fake.body_calls == ["m1"]  # bodies only for prefilter survivors

    timeline = client.get(f"{API}/applications/{acme}/events", headers=headers).json()["events"]
    last = timeline[-1]
    assert last["status"] == "rejected" and last["status_source"] == "email_parser"
    assert last["note"].startswith("On ") and "Acme Corp told you it will not move forward" in last["note"]
    assert "Backend Engineer" in last["note"] and "Unfortunately" not in last["note"]  # template, no quoting
    assert last["email"]["subject"] == "Your application to Acme Corp"

    # idempotent: a second sync with nothing new changes nothing and calls no AI
    calls = len(ai.calls)
    fake.history_new = []
    assert _sync(account, fake)["new"] == 0 and len(ai.calls) == calls
    assert len(_events(user_id)) == 1

    # bodies are never stored anywhere
    db = SessionLocal()
    try:
        ev = db.query(EmailEvent).filter_by(user_id=user_id).one()
        assert BODY_MARKER not in json.dumps({c.name: str(getattr(ev, c.name)) for c in ev.__table__.columns})
        assert BODY_MARKER not in (db.get(Application, acme).last_email_snippet or "")
    finally:
        db.close()


def test_unclear_emails_go_to_review_and_can_be_resolved(client, make_user, ai):
    user_id, headers = make_user()
    a = _applied(user_id, "Acme Corp", "Backend Engineer")
    b = _applied(user_id, "Beta Labs", "Backend Engineer")
    account = _account(user_id)
    fake = FakeGmail()
    fake.add("m1", sender="Talent <talent@recruit.example>", subject="Interview for the Backend Engineer role",
             text="We'd like to schedule an interview for the Backend Engineer role.")  # fits both equally
    fake.add("m2", sender="hr@acmecorp.com", subject="Acme Corp application update",
             text="An update on your Backend Engineer application at Acme Corp.")
    ai.table["interview"] = INTERVIEW
    ai.table["update"] = {**REJECTION, "confidence": 0.4}  # matched, but the AI isn't sure

    _sync(account, fake)
    review = client.get(f"{API}/gmail/review", headers=headers).json()
    assert review["total"] == 2
    by_subject = {e["subject"]: e for e in review["events"]}
    unclear = by_subject["Interview for the Backend Engineer role"]
    assert {s["application_id"] for s in unclear["suggestions"]} == {a, b}
    assert _app(a).status == "submitted" and _app(b).status == "submitted"

    r = client.post(f"{API}/gmail/review/{unclear['id']}/resolve",
                    json={"action": "change", "application_id": b}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "applied" and r.json()["applied_status"] == "interview"
    assert _app(b).status == "interview"
    low = by_subject["Acme Corp application update"]
    assert low["suggestions"][0]["application_id"] == a
    assert client.post(f"{API}/gmail/review/{low['id']}/resolve", json={"action": "ignore"},
                       headers=headers).json()["state"] == "ignored"
    assert _app(a).status == "submitted"
    _, other = make_user()
    assert client.post(f"{API}/gmail/review/{low['id']}/resolve", json={"action": "ignore"},
                       headers=other).status_code == 404


def test_status_never_moves_backwards_and_threads_match(make_user, ai):
    user_id, _ = make_user()
    app_id = _applied(user_id, "Acme Corp", "Backend Engineer", status="interview")
    account = _account(user_id)
    fake = FakeGmail()
    fake.add("m1", sender="jobs@acmecorp.com", subject="Acme Corp received your application",
             text="Acme Corp received your Backend Engineer application.", thread="T1", days_ago=2)
    ai.table["received"] = {"category": "acknowledgement", "confidence": 0.9, "company": "Acme Corp",
                            "role": "Backend Engineer", "summary": "Application received."}
    _sync(account, fake)
    assert _app(app_id).status == "interview"  # an acknowledgement after an interview is only noted

    # a reply in the same thread that names nothing still matches through the thread
    fake.add("m2", sender="someone@mailer.example", subject="Re: next round", text="Please pick a time.",
             thread="T1", days_ago=1)
    fake.history_new = ["m2"]
    ai.table["next round"] = {"category": "offer", "confidence": 0.9, "company": "", "role": "", "summary": "An offer."}
    _sync(account, fake)
    assert _app(app_id).status == "offer"
    db = SessionLocal()
    try:
        notes = [e.note for e in db.get(Application, app_id).events]
    finally:
        db.close()
    assert any("status kept: interview" in (n or "") for n in notes)


def test_no_ai_model_keeps_the_email_pending_and_retries(make_user, ai):
    user_id, _ = make_user()
    app_id = _applied(user_id, "Acme Corp", "Backend Engineer")
    account = _account(user_id)
    fake = FakeGmail()
    fake.add("m1", sender="no-reply@greenhouse.io", subject="Acme Corp: your application",
             text="Acme Corp Backend Engineer. Unfortunately not moving forward.")
    ai.table["acme"] = REJECTION
    ai.down = True
    assert _sync(account, fake)["pending"] == 1
    assert _events(user_id)[0].state == "pending" and _app(app_id).status == "submitted"
    ai.down = False
    fake.history_new = []
    _sync(account, fake)
    assert _events(user_id)[0].state == "applied" and _app(app_id).status == "rejected"


def test_history_fallback_and_revoked_grant(make_user, ai):
    user_id, _ = make_user()
    account = _account(user_id)
    fake = FakeGmail()
    _sync(account, fake)
    assert fake.searches and fake.searches[0].startswith("newer_than:30d")
    fake.history_expired = True
    _sync(account, fake)
    assert fake.searches[-1].startswith("after:")

    fake.revoked = True
    fake.history_expired = False
    db = SessionLocal()
    try:
        db.get(EmailAccount, account).last_history_id = None
        db.commit()
    finally:
        db.close()
    assert _sync(account, fake)["error"] == "needs_reconnect"
    db = SessionLocal()
    try:
        row = db.get(EmailAccount, account)
        assert row.status == "needs_reconnect" and "reconnect" in row.last_error
    finally:
        db.close()
    assert _sync(account, fake)["skipped"] == "not active"


def test_due_accounts_are_claimed_once(make_user):
    user_id, _ = make_user()
    account = _account(user_id)
    db = SessionLocal()
    try:
        first = email_sync.claim_due(db)
        second = email_sync.claim_due(db)
    finally:
        db.close()
    assert account in first and account not in second


# ---------------------------------------------------------------- job-alert emails

def test_naukri_alert_emails_become_jobs_for_searches(make_user, ai):
    user_id, _ = make_user()
    account = _account(user_id)
    fake = FakeGmail()
    fake.add("a1", sender="Naukri <jobalert@naukri.com>", subject="Jobs for you: Python Developer", html=NAUKRI_ALERT)
    out = _sync(account, fake)
    assert out["job_alerts"] == 1 and ai.calls == []  # alerts are parsed without AI

    db = SessionLocal()
    try:
        jobs = {j.title: j for j in db.query(EmailJob).filter_by(user_id=user_id).all()}
        inbox = email_sync.recent_alert_jobs(db, user_id, 24)
    finally:
        db.close()
    assert set(jobs) == {"Python Developer", "Data Engineer"}
    assert jobs["Python Developer"].company == "Orbit Labs" and jobs["Python Developer"].location == "Pune"
    assert jobs["Data Engineer"].url.startswith("https://www.naukri.com/job-listings-data-engineer")

    found = job_sources.scrape_all(["Python Developer"], ["Pune"], ["email_alerts"], inbox_jobs=inbox,
                                   max_age_hours=72)
    assert [j["title"] for j in found] == ["Python Developer"] and found[0]["source"] == "Naukri"


# ---------------------------------------------------------------- disconnect, retention, stats

def test_disconnect_revokes_and_deletes_synced_data(client, make_user, ai, monkeypatch):
    user_id, headers = make_user()
    app_id = _applied(user_id, "Acme Corp", "Backend Engineer")
    account = _account(user_id)
    fake = FakeGmail()
    fake.add("m1", sender="no-reply@greenhouse.io", subject="Acme Corp update",
             text="Acme Corp Backend Engineer: unfortunately no.")
    fake.add("a1", sender="jobalert@naukri.com", subject="Jobs for you", html=NAUKRI_ALERT)
    ai.table["acme"] = REJECTION
    _sync(account, fake)
    revoked: list[str] = []
    monkeypatch.setattr(gmail, "revoke", lambda token: revoked.append(token))

    assert client.delete(f"{API}/gmail/accounts/{account}", headers=headers).status_code == 204
    assert revoked == ["refresh-123"]
    db = SessionLocal()
    try:
        assert db.query(EmailEvent).filter_by(user_id=user_id).count() == 0
        assert db.query(EmailJob).filter_by(user_id=user_id).count() == 0
    finally:
        db.close()
    assert _app(app_id).status == "rejected"  # the application keeps its history
    assert client.delete(f"{API}/gmail/accounts/{account}", headers=headers).status_code == 404


def test_retention_purges_old_events_and_alert_jobs(make_user):
    user_id, _ = make_user()
    account = _account(user_id)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=200)
    db = SessionLocal()
    try:
        db.add(EmailEvent(user_id=user_id, account_id=account, message_id="old", sender="x", subject="s", snippet="",
                          received_at=old, state="ignored", suggestions=[], created_at=old))
        db.add(EmailJob(user_id=user_id, account_id=account, source="Naukri", title="Old", url="https://x/old",
                        received_at=old))
        db.commit()
        out = email_sync.purge_old(db)
        assert out["events"] >= 1 and out["jobs"] >= 1
        assert db.query(EmailEvent).filter_by(user_id=user_id).count() == 0
    finally:
        db.close()


def test_tracker_stats_funnel(client, make_user):
    user_id, headers = make_user()
    a = _applied(user_id, "A", "Dev")
    _applied(user_id, "B", "Dev", status="acknowledged")
    _applied(user_id, "C", "Dev", status="interview")
    _applied(user_id, "D", "Dev", status="opened")  # never sent: not counted
    record_status(user_id=user_id, job={"url": "https://jobs.example/A/Dev", "title": "Dev", "company": "A"},
                  status="interview", status_source="email_parser")
    record_status(user_id=user_id, job={"url": "https://jobs.example/A/Dev", "title": "Dev", "company": "A"},
                  status="rejected", status_source="email_parser")
    s = client.get(f"{API}/applications/stats", headers=headers).json()
    assert s["total"] == 3 and s["this_week"] == 3
    assert s["funnel"] == {"applied": 3, "acknowledged": 3, "assessment": 2, "interview": 2, "offer": 0}
    assert s["rejected"] == 1 and s["by_status"]["opened"] == 1 and s["by_source"] == {"LinkedIn": 3}
    assert _app(a).status == "rejected"
    r = client.patch(f"{API}/applications/{a}", json={"status": "withdrawn"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"


def test_alert_source_runs_only_with_gmail_connected(client, make_user, dispatched):
    user_id, headers = make_user()
    providers = {p["key"]: p for p in client.get(f"{API}/providers", headers=headers).json()["sources"]}
    assert not providers["email_alerts"]["available"] and "connect Gmail" in providers["email_alerts"]["reason"]
    _account(user_id)
    providers = {p["key"]: p for p in client.get(f"{API}/providers", headers=headers).json()["sources"]}
    assert providers["email_alerts"]["available"]
