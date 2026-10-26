"""P4c — the AI form reader (generic adapter): detection, opt-in, caps and test mode, then real Chromium on
local pages: a one-page form (newsletter box and hidden text ignored), a three-page form behind an Apply
button with a question the user answers half-way, a custom widget fixed by the screenshot fallback after
form_map picked the Submit button, a sign-in page, and the fallback's refusals and limits."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from app.apply import vision
from app.apply.adapters import HardStop
from app.apply.generic import GenericAdapter, RunTools
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ai import AiModel
from app.models.apply import ApplyProof, ApplyRun
from app.services import ai_router, apply_engine, apply_options
from tests.test_apply_engine import API, FIXTURES, _job, _ready_user, _step, browser

ASHBY = "https://jobs.ashbyhq.com/orbit/0b1c-backend"


# ---------------------------------------------------------------- detection and router

def test_generic_sites_are_detected():
    assert apply_options.detect_adapter(ASHBY) == "generic"
    assert apply_options.detect_adapter("https://apply.workable.com/acme/j/ABC/") == "generic"
    assert apply_options.detect_adapter("https://acme.wd3.myworkdayjobs.com/en-US/x/job/1") is None  # needs an account
    assert apply_options.detect_adapter("https://job-boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    # A Teamtailor career page on the company's own domain (company-ATS jobs keep the platform as source).
    job = {"source": "Teamtailor", "apply_url": "https://careers.acme.com/jobs/1-dev", "url": "https://careers.acme.com"}
    assert apply_options.adapter_for(job) == "generic"
    opts = apply_options.options({"apply_url": ASHBY, "url": "https://www.linkedin.com/jobs/view/1"})
    assert opts["adapter"] == "generic" and opts["auto"]["available"] and not opts["fast"]["available"]


def test_form_vision_only_on_models_that_list_it():
    gemini = AiModel(provider="gemini", tasks=["form_vision"])
    groq = AiModel(provider="groq", tasks=[])
    whisper = AiModel(provider="groq", tasks=["stt"])
    assert ai_router.serves(gemini, "form_vision") and ai_router.serves(gemini, "score")
    assert ai_router.serves(gemini, "form_map")
    assert not ai_router.serves(groq, "form_vision") and ai_router.serves(groq, "form_map")
    assert not ai_router.serves(whisper, "score") and not ai_router.serves(whisper, "form_vision")


def test_migration_adds_form_vision_to_gemini_rows():
    db = SessionLocal()
    try:
        rows = {m.model_id: m.tasks for m in db.query(AiModel).all()}
    finally:
        db.close()
    gemini = [t for mid, t in rows.items() if mid.startswith("gemini")]
    assert gemini and all("form_vision" in t for t in gemini)
    assert rows["gpt-5-mini"] == ["form_vision"]
    assert "form_vision" not in rows["openai/gpt-oss-120b:free"]  # text only


def test_site_lock():
    adapter = GenericAdapter()
    adapter.sites.add("acme.com")
    adapter.check_site(SimpleNamespace(url="https://careers.acme.com/jobs/1"))
    adapter.check_site(SimpleNamespace(url="https://jobs.ashbyhq.com/acme/1/application"))
    with pytest.raises(HardStop):
        adapter.check_site(SimpleNamespace(url="https://evil.example.net/collect"))


# ---------------------------------------------------------------- opt-in, caps, test mode (no browser)

def test_generic_needs_opt_in_and_has_its_own_cap(client, make_user, dispatched, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    job_id = _job("Orbit Ashby", apply_url=ASHBY)
    r = client.post(f"{API}/apply/runs", json={"job_id": job_id}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "generic_off"

    s = client.put(f"{API}/apply/settings", json={"allow_generic": True}, headers=headers).json()
    assert s["allow_generic"] and not s["generic_submit_enabled"] and "AI form reader" in s["generic_notice"]
    assert s["caps"]["generic_per_day"] == 5
    monkeypatch.setattr(get_settings(), "APPLY_GENERIC_DAILY_CAP", 0)
    r = client.post(f"{API}/apply/runs", json={"job_id": job_id}, headers=headers)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "generic_cap"
    monkeypatch.setattr(get_settings(), "APPLY_GENERIC_DAILY_CAP", 5)
    r = client.post(f"{API}/apply/runs", json={"job_id": job_id}, headers=headers)
    assert r.status_code == 202 and r.json()["adapter"] == "generic"
    assert dispatched[-1]["task"] == "job_finder.apply_prepare"

    # Turning the reader off cancels its active runs.
    client.put(f"{API}/apply/settings", json={"allow_generic": False}, headers=headers)
    assert client.get(f"{API}/apply/runs/{r.json()['id']}", headers=headers).json()["state"] == "cancelled"


# ---------------------------------------------------------------- real browser

@pytest.fixture
def generic_engine(monkeypatch, tmp_path):
    """Real browser and adapter; local documents; the form_answer AI knows nothing; form_map and form_vision
    are scripted per test (calls recorded)."""
    from tests.test_apply_engine import _docx
    resume = tmp_path / "Jane Doe Resume.docx"
    resume.write_bytes(_docx("Jane Doe"))
    monkeypatch.setattr(apply_engine, "_documents", lambda db, run, workdir: {"resume": str(resume)})
    monkeypatch.setattr(apply_engine, "within_hours", lambda s: True)
    calls: dict = {"map": [], "vision": [], "map_reply": None, "vision_reply": None}

    def complete(db, **kw):
        if kw["task"] == "form_map":
            calls["map"].append(kw["user_prompt"])
            if calls["map_reply"] is None:
                raise ai_router.NoModelAvailable("form_map", [])
            return SimpleNamespace(text=calls["map_reply"](kw["user_prompt"]))
        kw["validate"](json.dumps({"answers": []}))
        return SimpleNamespace(engine="test", text="")

    def stream(db, **kw):
        assert kw["task"] == "form_vision" and kw["images"][0][0] == "image/png"
        calls["vision"].append(kw["user_prompt"])
        if calls["vision_reply"] is None:
            raise ai_router.NoModelAvailable("form_vision", [])
        kw["outcome"].text = calls["vision_reply"](kw["user_prompt"])
        kw["outcome"].cost_usd = 0.002
        yield kw["outcome"].text

    monkeypatch.setattr(ai_router, "complete", complete)
    monkeypatch.setattr(ai_router, "stream", stream)
    return calls


def _generic_run(client, headers, user_id: str, url: str, company: str) -> str:
    client.put(f"{API}/apply/settings", json={"allow_generic": True}, headers=headers)
    job_id = _job(company, apply_url=None)
    db = SessionLocal()
    try:
        run = ApplyRun(user_id=user_id, job_id=job_id, job_key=f"{company.lower()}|backend engineer|pune",
                       company_title=f"{company.lower()}|backend engineer", mode="auto", level=2, adapter="generic",
                       apply_url=url, state="queued", active_key=f"{user_id}|{company}", plan={})
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _submitted(run_id: str, monkeypatch) -> dict:
    """Runs submit and returns what the local page received."""
    captured: dict = {}
    real = GenericAdapter.submit_and_wait

    def spy(self, page, wait_ms):
        result = real(self, page, wait_ms)
        captured.update(page.evaluate("({form: window.__submitted || null, nl: window.__newsletter || null})"))
        return result

    monkeypatch.setattr(GenericAdapter, "submit_and_wait", spy)
    run = _step("submit", run_id)
    captured["run"] = run
    return captured


def _proof_kinds(run_id: str) -> list[str]:
    db = SessionLocal()
    try:
        return [p.kind for p in db.query(ApplyProof).filter_by(run_id=run_id).order_by(ApplyProof.created_at)]
    finally:
        db.close()


@browser
def test_one_page_form_end_to_end(client, make_user, generic_engine, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    run_id = _generic_run(client, headers, user_id, (FIXTURES / "generic_single.html").as_uri(), "Orbit One")
    run = _step("prepare", run_id)
    assert run.state == "awaiting_input", run.message
    out = client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()
    fields = {f["key"]: f for f in out["fields"]}
    assert "nl" not in fields  # the newsletter box is not part of the application form
    assert fields["_systemfield_name"]["value"] == "Jane Doe" and fields["auth"]["value"] == "Yes"
    assert fields["why"]["needs_input"] and out["files"] == {"_systemfield_resume": "resume"}
    assert [s["page"] for s in out["steps"]] == [1] and generic_engine["map"] == []  # no AI needed to read it

    run = client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"why": "Your product."}},
                      headers=headers).json()
    assert run["state"] == "awaiting_review" and "test mode" in run["message"]
    r = client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "generic_test_mode"

    monkeypatch.setattr(get_settings(), "GENERIC_APPLY_SUBMIT", True)
    assert client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers).json()["state"] == "submitting"
    got = _submitted(run_id, monkeypatch)
    assert got["run"].state == "submitted", got["run"].message
    assert got["form"] == {"name": "Jane Doe", "email": "jane@example.com", "resume": "Jane Doe Resume.docx",
                           "auth": "yes", "why": "Your product."}
    assert got["nl"] in ("", None)
    assert _proof_kinds(run_id)[-1] == "confirmation"


@browser
def test_three_page_form_with_a_question_half_way(client, make_user, generic_engine, monkeypatch, dispatched):
    user_id, headers = _ready_user(client, make_user)
    run_id = _generic_run(client, headers, user_id, (FIXTURES / "generic_multistep.html").as_uri(), "Nimbus")
    run = _step("prepare", run_id)  # Apply button -> page 1 filled -> page 2 asks "Why Nimbus?"
    assert run.state == "awaiting_input" and "Why Nimbus" in run.message, run.message
    assert [s["page"] for s in run.plan["steps"]] == [1, 2] and not run.plan["complete"]

    r = client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"why": "Distributed systems."}},
                    headers=headers).json()
    assert r["state"] == "queued" and dispatched[-1]["task"] == "job_finder.apply_prepare"
    run = _step("prepare", run_id)  # reads all three pages with the answer kept
    assert run.state == "awaiting_review", run.message
    assert [s["page"] for s in run.plan["steps"]] == [1, 2, 3] and run.plan["complete"]
    fields = {f["key"]: f for f in client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()["fields"]}
    assert fields["fn"]["value"] == "Jane" and fields["np"]["value"] == "30 days"
    assert fields["gender"]["value"] == "Female" and fields["why"]["source"] == "user"
    assert _proof_kinds(run_id).count("step") == 5  # pages 1-2, then 1-3 again

    monkeypatch.setattr(get_settings(), "GENERIC_APPLY_SUBMIT", True)
    client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    got = _submitted(run_id, monkeypatch)
    assert got["run"].state == "submitted", got["run"].message
    form = got["form"]
    assert (form["first_name"], form["last_name"], form["email"]) == ("Jane", "Doe", "jane@example.com")
    assert form["cv"] == "Jane Doe Resume.docx" and form["notice"] == "30 days" and form["why"] == "Distributed systems."
    assert form["gender"] == "Female"


def _option_mark(prompt: str, text: str) -> str | None:
    m = re.search(r"- (v\d+): div \"" + re.escape(text) + "\"", prompt)
    return m.group(1) if m else None


@browser
def test_custom_widget_is_fixed_by_the_screenshot_fallback(client, make_user, generic_engine, monkeypatch):
    user_id, headers = _ready_user(client, make_user)

    def map_reply(prompt: str) -> str:  # "Send it" is the submit button
        mark = re.search(r"- (b\d+): \[button\] Send it", prompt).group(1)
        return json.dumps({"page_kind": "form", "apply": None, "next": None, "submit": mark})

    generic_engine["map_reply"] = map_reply
    run_id = _generic_run(client, headers, user_id, (FIXTURES / "generic_custom.html").as_uri(), "Kite")
    run = _step("prepare", run_id)
    assert run.state == "awaiting_input" and "Country" in run.message, run.message
    assert len(generic_engine["map"]) == 1  # the button texts were ambiguous: one form_map call
    client.post(f"{API}/apply/runs/{run_id}/answer", json={"answers": {"country": "India"}}, headers=headers)

    seen: list[str] = []

    def vision_script(prompt: str) -> str:
        seen.append(prompt)
        if len(seen) == 1:  # open the picker
            return json.dumps({"action": "click", "mark": re.search(r"- (v\d+): input/combobox", prompt).group(1)})
        if len(seen) == 2:
            return json.dumps({"action": "choose", "mark": _option_mark(prompt, "India"), "key": "country"})
        return json.dumps({"action": "done"})

    generic_engine["vision_reply"] = vision_script
    monkeypatch.setattr(get_settings(), "GENERIC_APPLY_SUBMIT", True)
    client.post(f"{API}/apply/runs/{run_id}/approve", headers=headers)
    got = _submitted(run_id, monkeypatch)
    assert got["run"].state == "submitted", got["run"].message
    assert got["form"] == {"name": "Jane Doe", "email": "jane@example.com", "country": "IN"}
    usage = got["run"].plan["vision"]
    assert usage["steps"] == 3 and usage["cost_usd"] == pytest.approx(0.006)
    assert [e.get("action") for e in usage["log"]] == ["click", "choose", "done"]
    answers = seen[0].split("Answers you may use")[1].split("Elements on the screenshot")[0]
    assert answers.strip().splitlines()[1:] == ['- country: "Country *" = "India"']  # only this gap's answer


@browser
def test_sign_in_page_hands_the_run_over(client, make_user, generic_engine):
    user_id, headers = _ready_user(client, make_user)
    url = (FIXTURES / "generic_custom.html").as_uri() + "?login=1"
    run = _step("prepare", _generic_run(client, headers, user_id, url, "Kite Login"))
    assert run.state == "needs_user" and "sign in" in run.message


# ---------------------------------------------------------------- the fallback's own rules

@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.set_content('<input id="q" aria-label="Search jobs"><input id="c" aria-label="City">'
                       '<button type="submit">Submit application</button><button>Next</button>')
        yield pg
        b.close()


def _tools(*replies, cost: float = 0.0, **limits) -> RunTools:
    it = iter(replies)
    return RunTools(look=lambda png, prompt: (json.dumps(next(it)), cost), **limits)


def _mark(page, label: str) -> str:
    return next(m["mark"] for m in page.evaluate(vision._MARK_JS) if m["text"] == label)


@browser
def test_fallback_refuses_other_keys_and_submit_buttons(page):
    answers = {"city": {"label": "City", "value": "Pune"}}
    q = _mark(page, "Search jobs")
    # A key that isn't part of this gap (e.g. steered by page text) is refused; nothing is typed.
    tools = _tools({"action": "fill", "mark": q, "key": "email"})
    assert not vision.fix(page, tools, lambda p: None, goal="Set City", answers=answers)
    assert page.input_value("#q") == "" and tools.vision_log[-1]["refused"] == "key is not part of this step"
    # The button that sends the application is never clicked by the fallback.
    tools = _tools({"action": "click", "mark": _mark(page, "Submit application")})
    assert not vision.fix(page, tools, lambda p: None, goal="Set City", answers=answers)
    assert "won't click" in tools.vision_log[-1]["refused"]
    # The model can only fill a stored answer, never its own text.
    tools = _tools({"action": "fill", "mark": _mark(page, "City"), "key": "city"}, {"action": "done"})
    assert vision.fix(page, tools, lambda p: None, goal="Set City", answers=answers)
    assert page.input_value("#c") == "Pune"


@browser
def test_fallback_step_and_cost_limits(page):
    answers = {"city": {"label": "City", "value": "Pune"}}
    scroll = {"action": "scroll", "direction": "down"}
    tools = _tools(*[scroll] * 10, max_vision_steps=3)
    assert not vision.fix(page, tools, lambda p: None, goal="Set City", answers=answers)
    assert tools.vision_steps == 3 and tools.vision_log[-1] == {"stop": "step limit"}
    tools = _tools(*[scroll] * 10, cost=0.2, max_vision_usd=0.25)
    assert not vision.fix(page, tools, lambda p: None, goal="Set City", answers=answers)
    assert tools.vision_steps == 2 and tools.vision_log[-1] == {"stop": "cost limit"}
    # Leaving the site stops the run.
    tools = _tools(scroll)

    def leave(p):
        raise HardStop("another website")

    with pytest.raises(HardStop):
        vision.fix(page, tools, leave, goal="Set City", answers=answers)
