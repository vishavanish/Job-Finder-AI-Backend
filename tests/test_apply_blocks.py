"""QA5: sites that block the server's apply browser — the block page is recognised, the run is handed over with
a plain message, and Auto apply on that platform pauses for everyone (docs/plans/QA5-apply-blocked-sites.md)."""
from __future__ import annotations

import time

import pytest

from app.apply import forms
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.admin import AuditLog
from app.models.jobs import Job
from app.models.apply import ApplyRun
from app.services import apply_blocks, apply_options
from tests.test_apply_engine import API, FIXTURES, _job, _local_run, _ready_user, _step, browser, engine  # noqa: F401
from tests.test_generic_apply import _generic_run, _proof_kinds

SR_JOB = {"apply_url": "https://jobs.smartrecruiters.com/Sia/744000157093395", "url": "https://x.example/1",
          "source": "SmartRecruiters"}


# ---------------------------------------------------------------- recognising a block page

@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b.new_page()
        b.close()


@browser
def test_block_pages_are_recognised_and_job_posts_are_not(page):
    page.goto((FIXTURES / "blocked_access.html").as_uri())
    assert "Access is temporarily restricted" in forms.blocked(page)

    page.set_content("<h1>Attention Required! | Cloudflare</h1><p>Sorry, you have been blocked</p>")
    assert forms.blocked(page)
    page.set_content("<h1>Access Denied</h1><p>You don't have permission. Reference #18.2f3c</p>")
    assert forms.blocked(page)

    page.set_content("<h1>Backend Engineer</h1><p>Apply below.</p>")
    assert forms.blocked(page) is None
    assert forms.blocked(page, 403) == "The site answered HTTP 403."
    assert forms.blocked(page, 200) is None

    # A long job post that happens to use the words is not a block page.
    page.set_content("<h1>Trust & Safety Engineer</h1><p>" + "Build abuse defences. " * 200
                     + "Users see 'you have been blocked' when our systems detect automated (bot) activity.</p>")
    assert forms.blocked(page) is None


# ---------------------------------------------------------------- runs that hit a block

@browser
def test_apply_button_leading_to_a_block_page_hands_over_and_pauses(client, make_user, monkeypatch):
    user_id, headers = _ready_user(client, make_user)
    monkeypatch.setattr("app.services.apply_engine._documents", lambda db, run, workdir: {})
    run_id = _generic_run(client, headers, user_id, (FIXTURES / "blocked_job.html").as_uri(), "Sia")
    db = SessionLocal()
    try:
        run = db.get(ApplyRun, run_id)
        db.get(Job, run.job_id).best_source = "SmartRecruiters"  # a career-page job, like the live one
        db.commit()
    finally:
        db.close()

    run = _step("prepare", run_id)
    assert run.state == "needs_user"
    assert run.message.startswith("SmartRecruiters blocked the app's browser")
    assert "paused for 24 h" in run.message and run.error.startswith("Blocked: The page says")
    assert _proof_kinds(run_id) == ["stopped"]
    out = client.get(f"{API}/apply/runs/{run_id}", headers=headers).json()
    assert out["fields"] == [] and out["steps"] == []
    assert apply_blocks.paused("smartrecruiters")["run_id"] == run_id


@browser
def test_lever_block_page_pauses_lever(client, make_user, monkeypatch):
    user_id, _ = _ready_user(client, make_user)
    monkeypatch.setattr("app.services.apply_engine._documents", lambda db, run, workdir: {})
    run = _step("prepare", _local_run(user_id, "lever", (FIXTURES / "blocked_access.html").as_uri(), "Lever Block"))
    assert run.state == "needs_user" and run.message.startswith("Lever blocked the app's browser")
    assert apply_blocks.paused("lever") and apply_blocks.paused("greenhouse") is None


# ---------------------------------------------------------------- the pause

def test_platform_names():
    assert apply_blocks.platform_of("generic", SR_JOB["apply_url"]) == ("smartrecruiters", "SmartRecruiters")
    assert apply_blocks.platform_of("generic", "https://careers.acme.example/j/1", "Ashby") == ("ashby", "Ashby")
    assert apply_blocks.platform_of("greenhouse", "https://job-boards.greenhouse.io/a/jobs/1") == (
        "greenhouse", "Greenhouse")
    assert apply_blocks.platform_of("generic", "https://careers.acme.co.in/j/1") == ("acme.co.in", "acme.co.in")


def test_pause_doubles_on_repeat_and_is_capped(monkeypatch):
    assert apply_blocks.record("smartrecruiters", "SmartRecruiters")["hours"] == 24
    assert apply_blocks.record("smartrecruiters", "SmartRecruiters")["hours"] == 48
    for _ in range(4):
        row = apply_blocks.record("smartrecruiters", "SmartRecruiters")
    assert row["hours"] == get_settings().APPLY_BLOCK_MAX_PAUSE_HOURS == 168

    # A block more than a week after the last one starts again at 24 h.
    real = time.time
    monkeypatch.setattr(apply_blocks.time, "time", lambda: real() + 8 * 24 * 3600)
    assert apply_blocks.record("smartrecruiters", "SmartRecruiters")["hours"] == 24


def test_expired_pause_lifts_and_a_clean_page_forgets_it(monkeypatch):
    apply_blocks.record("ashby", "Ashby")
    assert apply_blocks.paused("ashby")
    real = time.time
    monkeypatch.setattr(apply_blocks.time, "time", lambda: real() + 25 * 3600)
    apply_blocks._forget_cache()
    assert apply_blocks.paused("ashby") is None
    apply_blocks.note_ok("ashby")
    assert apply_blocks.record("ashby", "Ashby")["hours"] == 24  # not counted as a repeat


def test_paused_platform_disables_auto_apply_for_everyone(client, make_user, engine, dispatched):
    assert apply_options.options(SR_JOB)["auto"]["available"] is True
    apply_blocks.record("smartrecruiters", "SmartRecruiters")
    auto = apply_options.options(SR_JOB)["auto"]
    assert auto["available"] is False
    assert auto["reason"].startswith("SmartRecruiters is blocking automated applications from our server")
    assert "in about 24 h" in auto["reason"]
    assert apply_options.options({**SR_JOB, "apply_url": "https://jobs.lever.co/a/1"})["auto"]["available"]

    apply_blocks.record("greenhouse", "Greenhouse")
    _, headers = _ready_user(client, make_user)
    r = client.post(f"{API}/apply/runs", json={"job_id": _job("Paused Co")}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "platform_blocked"
    assert "Greenhouse is blocking" in r.json()["detail"]["message"]
    lever = _job("Lever Co", apply_url="https://jobs.lever.co/lever-co/1")
    assert client.post(f"{API}/apply/runs", json={"job_id": lever}, headers=headers).status_code == 202


def test_admin_sees_and_clears_a_pause(client, make_user, make_admin):
    admin_id, admin = make_admin()
    apply_blocks.record("smartrecruiters", "SmartRecruiters", "run-1")
    health = client.get(f"{API}/admin/system/health", headers=admin).json()
    assert [(b["platform"], b["name"], b["hours"], b["run_id"]) for b in health["apply_blocked"]] == [
        ("smartrecruiters", "SmartRecruiters", 24, "run-1")]

    _, user = make_user()
    assert client.delete(f"{API}/admin/apply/blocks/smartrecruiters", headers=user).status_code == 403
    r = client.delete(f"{API}/admin/apply/blocks/smartrecruiters", headers=admin)
    assert r.status_code == 200 and r.json() == {"apply_blocked": []}
    assert apply_blocks.paused("smartrecruiters") is None
    assert client.delete(f"{API}/admin/apply/blocks/smartrecruiters", headers=admin).status_code == 404
    db = SessionLocal()
    try:
        entry = db.query(AuditLog).filter_by(action="apply.block_clear").one()
        assert entry.actor_id == admin_id and entry.target_id == "smartrecruiters" and entry.before["hours"] == 24
    finally:
        db.close()
