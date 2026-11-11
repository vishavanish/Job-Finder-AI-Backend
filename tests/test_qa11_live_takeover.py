"""QA11 A — "Sign in to continue": a run that meets a sign-in page waits in awaiting_login, the user signs in on
a live view (the real live browser service, real Chromium, a local site whose apply page needs a cookie),
and the run carries on with that browser's cookies. Plus the gates, the socket relay, give up, expiry and
the live browser's site guard."""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.live_browser import internal_key
from app.live_browser.session import Guard
from app.models.apply import ApplyRun
from app.services import live_takeover
from tests.test_apply_engine import API, FIXTURES, _local_run, _ready_user, _step, browser, real_browser_engine  # noqa: F401

LIVE = "http://127.0.0.1:8790"


@pytest.fixture
def site():
    """The apply fixtures over http://127.0.0.1 (cookies need http)."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def live_on(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "LIVE_BROWSER_URL", LIVE)
    monkeypatch.setattr(cfg, "LIVE_ALLOW_PRIVATE", True)
    return cfg


@pytest.fixture
def service(live_on, monkeypatch):
    """The real live browser service in-process; live_takeover talks to it through its test client."""
    from fastapi.testclient import TestClient

    from app.live_browser import service as svc
    with TestClient(svc.app) as c:
        def call(method, path, **kw):
            return c.request(method, path, headers={"X-Live-Key": internal_key()}, **kw)
        monkeypatch.setattr(live_takeover, "_call", call)
        yield c


def _waiting(user_id: str, url: str, company: str, phase: str = "prepare") -> str:
    run_id = _local_run(user_id, "greenhouse", url, company)
    db = SessionLocal()
    try:
        run = db.get(ApplyRun, run_id)
        live_takeover.begin(db, run, kind="login", reason="The application page asks you to sign in.", url=url,
                            phase=phase)
        run.state = "awaiting_login"
        db.commit()
    finally:
        db.close()
    return run_id


# ---------------------------------------------------------------- end to end with real Chromium

@browser
def test_sign_in_on_the_live_view_then_the_run_carries_on(client, make_user, real_browser_engine, site, service,
                                                          dispatched):
    user_id, headers = _ready_user(client, make_user)
    run_id = _local_run(user_id, "lever", f"{site}/login_gate.html", "Orbit Live")

    run = _step("prepare", run_id)
    assert run.state == "awaiting_login", run.message
    assert "asks you to sign in" in run.message and "live view" in run.message
    body = client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()
    t = body["takeover"]
    assert t["kind"] == "login" and t["site"] == "127.0.0.1" and t["available"] and t["sessions_left_today"] == 10
    waiting = client.get(f"{API}/apply/runs?state=waiting", headers=headers).json()["runs"]
    assert [r["id"] for r in waiting] == [run_id]

    start = client.post(f"{API}/apply/runs/{run_id}/live", headers=headers)
    assert start.status_code == 200, start.text
    assert start.json()["width"] == 1280 and start.json()["session_minutes"] == 15

    # The user types into the live browser (what the API socket relays): email, Tab, password, Enter.
    with service.websocket_connect(f"/sessions/{run_id}/stream", headers={"X-Live-Key": internal_key()}) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello" and hello["url"].endswith("/login_gate.html")
        got_frame, ready = False, False
        for msg in [{"type": "text", "text": "jane@example.com"}, {"type": "key", "key": "Tab"},
                    {"type": "text", "text": "not-stored-anywhere"}, {"type": "key", "key": "Enter"}]:
            ws.send_text(json.dumps(msg))
        for _ in range(80):
            m = ws.receive()
            if m.get("bytes"):
                got_frame = got_frame or m["bytes"][:2] == b"\xff\xd8"  # JPEG
            elif m.get("text"):
                ev = json.loads(m["text"])
                if ev["type"] == "state" and ev["ready"]:
                    ready = True
            if ready and got_frame:
                break
        assert got_frame and ready

    cont = client.post(f"{API}/apply/runs/{run_id}/live/continue", headers=headers)
    assert cont.status_code == 200, cont.text
    assert cont.json()["state"] == "queued" and cont.json()["takeover"] is None
    assert any(c["task"] == "job_finder.apply_prepare" and c["args"] == [run_id] for c in dispatched)
    state = live_takeover.storage_state(run_id)
    assert any(c["name"] == "jf_session" for c in state["cookies"])

    # The engine opens its own browser with those cookies: straight to the form this time.
    run = _step("prepare", run_id)
    assert run.state == "awaiting_review", run.message
    fields = {f["key"]: f for f in client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["fields"]}
    assert fields["name"]["value"] == "Jane Doe"
    from app.live_browser.service import SESSIONS
    assert run_id not in SESSIONS


@browser
def test_live_browser_stays_on_the_job_site(site, live_on):
    """The site guard inside a real session: a link to another site is not opened."""
    from app.live_browser.session import LiveSession

    async def go():
        s = LiveSession("g1", f"{site}/login_gate.html", set(), allow_private=True)
        events = []

        async def send(m):
            if isinstance(m, dict):
                events.append(m)
        s._sender = send
        await s.start()
        await s.page.click("#away")
        await asyncio.sleep(0.5)
        url = s.page.url
        await s.close()
        return events, url

    events, url = asyncio.run(go())
    assert url.endswith("/login_gate.html")
    assert any(e["type"] == "blocked" and e["host"] == "evil.example.net" for e in events)


def test_guard_rules():
    g = Guard({"acme.wd3.myworkdayjobs.com"})
    assert g.site_allowed("https://acme.wd3.myworkdayjobs.com/en-US/careers/login")
    assert g.site_allowed("https://other.myworkdayjobs.com/x")  # same registrable site
    assert g.site_allowed("https://accounts.google.com/o/oauth2")  # sign-in provider
    assert not g.site_allowed("https://evil.example.net/")
    assert not g.site_allowed("file:///etc/passwd") and not g.site_allowed("javascript:alert(1)")
    assert asyncio.run(g.private_host("http://127.0.0.1:8000/admin"))
    assert asyncio.run(g.private_host("http://169.254.169.254/latest/meta-data"))
    assert asyncio.run(g.private_host("http://localhost/"))
    assert asyncio.run(g.private_host("http://10.0.0.5/"))
    assert not asyncio.run(g.private_host("data:text/html,hi"))


# ---------------------------------------------------------------- gates and run states (no browser)

def test_off_unless_set_up_and_switched_on(client, make_user, monkeypatch):
    user_id, _ = _ready_user(client, make_user)
    run_id = _local_run(user_id, "greenhouse", "https://boards.greenhouse.io/orbit/jobs/1", "Gate Co")
    db = SessionLocal()
    try:
        run = db.get(ApplyRun, run_id)
        assert not live_takeover.eligible(db, run, "login")  # LIVE_BROWSER_URL empty: off
        monkeypatch.setattr(get_settings(), "LIVE_BROWSER_URL", LIVE)
        assert live_takeover.eligible(db, run, "login") and live_takeover.eligible(db, run, "captcha")
        assert not live_takeover.eligible(db, run, None)  # e.g. "no form found": nothing to sign in to
        from app.models.admin import FeatureFlag
        db.merge(FeatureFlag(key="live_takeover_enabled", value=False))
        db.commit()
        assert not live_takeover.eligible(db, run, "login")
        db.query(FeatureFlag).delete()
        db.commit()
        run.plan = {"takeovers": 2}
        assert not live_takeover.eligible(db, run, "login")  # asked twice already
        run.plan = {"runner": "extension"}
        assert not live_takeover.eligible(db, run, "login")
        run.plan, run.mode = {}, "fast"
        assert not live_takeover.eligible(db, run, "login")
    finally:
        db.rollback()
        db.close()


def test_start_errors_and_daily_cap(client, make_user, live_on, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    run_id = _waiting(user_id, "https://acme.example.com/login", "Cap Co")
    calls = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    answers = {"busy": Resp(409, {}), "ok": Resp(200, {"created": True, "width": 1280, "height": 860})}
    mode = {"v": "busy"}
    monkeypatch.setattr(live_takeover, "_call", lambda m, p, **kw: calls.append((m, p, kw)) or answers[mode["v"]])
    r = client.post(f"{API}/apply/runs/{run_id}/live", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "live_busy"
    assert calls[0][2]["json"]["allowed_sites"] == ["acme.example.com"]
    assert calls[0][2]["json"]["url"] == "https://acme.example.com/login"
    mode["v"] = "ok"
    monkeypatch.setattr(get_settings(), "LIVE_SESSION_DAILY_CAP", 1)
    assert client.post(f"{API}/apply/runs/{run_id}/live", headers=headers).status_code == 200
    r = client.post(f"{API}/apply/runs/{run_id}/live", headers=headers)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "live_daily_cap"

    _, other = make_user()
    assert client.post(f"{API}/apply/runs/{run_id}/live", headers=other).status_code == 404


def test_continue_needs_an_open_session_and_submit_phase_resumes_submitting(client, make_user, live_on, monkeypatch,
                                                                            dispatched):
    user_id, headers = _ready_user(client, make_user)

    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body or {}

        def json(self):
            return self._b

    monkeypatch.setattr(live_takeover, "_call", lambda m, p, **kw: Resp(404))
    run_id = _waiting(user_id, "https://acme.example.com/login", "Ended Co")
    r = client.post(f"{API}/apply/runs/{run_id}/live/continue", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "live_ended"

    monkeypatch.setattr(live_takeover, "_call",
                        lambda m, p, **kw: Resp(200, {"storage_state": {"cookies": [{"name": "s"}], "origins": []}}))
    sub = _waiting(user_id, "https://acme2.example.com/login", "Submit Co", phase="submit")
    r = client.post(f"{API}/apply/runs/{sub}/live/continue", headers=headers)
    assert r.json()["state"] == "submitting"
    assert any(c["task"] == "job_finder.apply_submit" and c["args"] == [sub] for c in dispatched)
    assert live_takeover.storage_state(sub)["cookies"] == [{"name": "s"}]
    assert client.post(f"{API}/apply/runs/{sub}/live/continue", headers=headers).status_code == 409


def test_give_up_and_expiry(client, make_user, live_on, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    monkeypatch.setattr(live_takeover, "_call", lambda m, p, **kw: None)
    run_id = _waiting(user_id, "https://acme.example.com/login", "Give Co")
    r = client.post(f"{API}/apply/runs/{run_id}/live/give-up", headers=headers)
    assert r.json()["state"] == "needs_user" and "yourself" in r.json()["message"]

    old = _waiting(user_id, "https://acme3.example.com/login", "Old Co")
    db = SessionLocal()
    try:
        run = db.get(ApplyRun, old)
        then = (datetime.now(timezone.utc) - timedelta(minutes=121)).isoformat()
        run.plan = {**run.plan, "takeover": {**run.plan["takeover"], "started_at": then}}
        db.commit()
    finally:
        db.close()
    body = client.get(f"{API}/apply/runs/{old}", headers=headers).json()
    assert body["state"] == "needs_user" and "in time" in body["message"]


def test_socket_relays_frames_and_input(client, make_user, live_on, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    run_id = _waiting(user_id, "https://acme.example.com/login", "Socket Co")
    sent: list[str] = []

    class Upstream:
        def __init__(self):
            self.out = [b"\xff\xd8frame", json.dumps({"type": "state", "ready": False, "asks": ["password"]})]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.out:
                return self.out.pop(0)
            await asyncio.sleep(0.3)
            if len(sent) >= 2:
                raise StopAsyncIteration  # the service closed the session
            return json.dumps({"type": "noop"})

        async def send(self, text):
            sent.append(text)

    monkeypatch.setattr(live_takeover, "connect_service", lambda rid: Upstream())
    with client.websocket_connect(f"{API}/apply/runs/{run_id}/live") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "nope"}))
        err = json.loads(ws.receive_text())
        assert err["code"] == "auth_failed"

    token = live_takeover.issue_token(user_id, run_id)
    with client.websocket_connect(f"{API}/apply/runs/{run_id}/live") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        assert ws.receive_bytes() == b"\xff\xd8frame"
        assert json.loads(ws.receive_text())["asks"] == ["password"]
        ws.send_text(json.dumps({"type": "text", "text": "jane@example.com"}))
        ws.send_text("x" * 5000)  # too big: dropped
        ws.send_text(json.dumps({"type": "key", "key": "Enter"}))
        while True:
            msg = json.loads(ws.receive_text())
            if msg["type"] == "error":
                assert msg["code"] == "live_ended"
                break
    assert [json.loads(s)["type"] for s in sent] == ["text", "key"]
    # a used token can't be used again
    with client.websocket_connect(f"{API}/apply/runs/{run_id}/live") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        assert json.loads(ws.receive_text())["code"] == "auth_failed"


def test_service_needs_the_internal_key(live_on):
    from fastapi.testclient import TestClient

    from app.live_browser import service as svc
    with TestClient(svc.app) as c:
        assert c.get("/health").status_code == 401
        assert c.get("/health", headers={"X-Live-Key": "wrong"}).status_code == 401
        assert c.get("/health", headers={"X-Live-Key": internal_key()}).json() == {"sessions": 0, "max": 2}
        assert c.post("/sessions/none/finish", headers={"X-Live-Key": internal_key()}).status_code == 404
