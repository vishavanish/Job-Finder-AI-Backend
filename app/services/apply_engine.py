"""
app/services/apply_engine.py
-------------------------------
The apply engine (CLAUDE.md 7.7, P4 plan §3.1, §3.6, §3.7).

    Preflight -> Prepare -> (Answer) -> Review -> Submit -> Verify -> Record

Two modes:
  auto  Level 2 "review & submit" on Greenhouse / Lever, and on other hosted
        career forms through the AI form reader (adapter "generic", P4c:
        opt-in, may be several pages, test mode until GENERIC_APPLY_SUBMIT),
        headless on the apply-queue worker. The run stops at `awaiting_review` (and at
        `awaiting_input` first when a required answer is missing); nothing
        is submitted until the user approves that run.
  fast  LinkedIn Easy Apply on the user's own PC: a visible browser opens,
        the first step is filled, and the user checks and submits it
        themselves (`needs_user`). Never in production (needs the P8 agent).

The browser is not kept between steps: prepare and submit each open the
page, read the form again and match fields by their id/name.

Safety (all enforced here, not only in the UI):
  - kill switches: global `auto_apply_enabled`, the user's own pause;
    checked when a run starts and again before it submits;
  - entitlement `auto_apply` max_level >= 2, the user's level 2 and the
    accepted risk notice for auto runs;
  - caps per day (all / per platform / Fast apply), one application per
    company + title in APPLY_COMPANY_COOLDOWN_DAYS, never the same job
    twice, the user's apply hours, 30-90 s between submits on a platform;
  - hard stops (login, captcha, unknown page, errors, an unclear result)
    end in `needs_user` with a screenshot — never retried or worked around.
Every state change is written to the tracker through record_status
(status_source "apply_engine").
"""
from __future__ import annotations

import contextlib
import logging
import random
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.apply import adapters as adapter_registry
from app.apply import vision
from app.apply.adapters import Blocked, HardStop
from app.apply.generic import MAP_SYSTEM, RunTools, valid_map
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.application import Application
from app.models.apply import (
    RUN_ACTIVE_STATES, AnswerBankEntry, ApplyProof, ApplyRun, ApplySettings,
)
from app.models.jobs import Job
from app.models.profile import Resume
from app.models.user import User
from app.services import (
    ai_router, answer_bank, apply_blocks, apply_options, apply_prep, entitlements, feature_flags, job_store, key_store,
    resume_store,
)
from app.services.applications_store import record_status
from app.services.job_dedupe import job_key as compute_job_key
from app.services.secret_refs import master_fernet

logger = logging.getLogger("job_finder_api.apply_engine")

APPLIED_STATUSES = {"submitted", "emailed", "acknowledged", "assessment", "interview", "offer", "accepted",
                    "declined", "rejected"}
_RESUME_LABEL = re.compile(r"\b(resume|résumé|cv|curriculum)\b", re.I)
_COVER_LABEL = re.compile(r"\bcover\s*letter\b", re.I)
_EEO_LABEL = re.compile(r"\b(gender|sex|race|ethnic\w*|veteran|disabilit\w*|pronouns?)\b", re.I)


def _error(code: int, err: str, message: str, **details) -> HTTPException:
    return HTTPException(code, detail={"code": err, "message": message, **details})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- settings

def get_settings_row(db: Session, user_id: str) -> ApplySettings:
    row = db.get(ApplySettings, user_id)
    if row is None:
        row = ApplySettings(user_id=user_id, level=1, hours_start=8, hours_end=22, timezone="Asia/Kolkata",
                            paused=False)
        db.add(row)
        db.flush()
    return row


def update_settings(db: Session, user: User, *, level: int | None, accept_risk: bool | None,
                    hours_start: int | None, hours_end: int | None, tz: str | None,
                    paused: bool | None, allow_generic: bool | None = None) -> ApplySettings:
    row = get_settings_row(db, user.id)
    if accept_risk is True and row.risk_accepted_at is None:
        row.risk_accepted_at = _now()
    elif accept_risk is False:
        row.risk_accepted_at = None
    if level is not None:
        if level >= 2:
            max_level = entitlements.effective(db, user.id, "auto_apply").max_level or 1
            if max_level < 2:
                raise _error(403, "level_not_allowed",
                             "Your account allows level 1 (Assist) only. Ask an admin to allow level 2.")
            if row.risk_accepted_at is None:
                raise _error(409, "risk_notice_required",
                             "Accept the auto-apply risk notice before turning on level 2.")
        row.level = level
    if tz is not None:
        try:
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            raise _error(422, "bad_timezone", f"Unknown time zone {tz!r}.")
        row.timezone = tz
    if hours_start is not None:
        row.hours_start = hours_start
    if hours_end is not None:
        row.hours_end = hours_end
    if row.hours_start >= row.hours_end:
        raise _error(422, "bad_hours", "Apply hours must start before they end.")
    if paused is not None:
        row.paused = paused
        if paused:
            cancel_active_runs(db, user.id, reason="You paused auto apply.")
    if allow_generic is not None:
        row.allow_generic = allow_generic
        if not allow_generic:
            for run in db.query(ApplyRun).filter(ApplyRun.user_id == user.id, ApplyRun.adapter == "generic",
                                                 ApplyRun.state.in_(RUN_ACTIVE_STATES)).all():
                _transition(db, run, "cancelled", "You turned off the AI form reader.", tracker_status="cancelled")
    return row


def _local_now(s: ApplySettings) -> datetime:
    return _now().astimezone(ZoneInfo(s.timezone))


def within_hours(s: ApplySettings) -> bool:
    return s.hours_start <= _local_now(s).hour < s.hours_end


def _day_start_utc(s: ApplySettings) -> datetime:
    local = _local_now(s).replace(hour=0, minute=0, second=0, microsecond=0)
    return _naive(local)


# ---------------------------------------------------------------- caps and dedupe

def usage_today(db: Session, user_id: str, s: ApplySettings) -> dict:
    start = _day_start_utc(s)
    submitted = (db.query(ApplyRun.adapter, func.count(ApplyRun.id))
                 .filter(ApplyRun.user_id == user_id, ApplyRun.mode == "auto", ApplyRun.submitted_at >= start)
                 .group_by(ApplyRun.adapter).all())
    by_platform = {a: n for a, n in submitted}
    fast = (db.query(func.count(ApplyRun.id))
            .filter(ApplyRun.user_id == user_id, ApplyRun.mode == "fast", ApplyRun.created_at >= start).scalar() or 0)
    return {"auto_submitted": sum(by_platform.values()), "by_platform": by_platform, "fast": fast}


def _check_caps(db: Session, user_id: str, s: ApplySettings, *, mode: str, adapter: str) -> None:
    cfg = get_settings()
    used = usage_today(db, user_id, s)
    if mode == "fast":
        if used["fast"] >= cfg.FAST_APPLY_DAILY_CAP:
            raise _error(429, "fast_cap", f"Fast apply limit reached for today ({cfg.FAST_APPLY_DAILY_CAP}/day).")
        return
    if used["auto_submitted"] >= cfg.APPLY_DAILY_CAP:
        raise _error(429, "daily_cap", f"Auto apply limit reached for today ({cfg.APPLY_DAILY_CAP}/day).")
    if adapter == "generic" and used["by_platform"].get("generic", 0) >= cfg.APPLY_GENERIC_DAILY_CAP:
        raise _error(429, "generic_cap",
                     f"AI form reader limit reached for today ({cfg.APPLY_GENERIC_DAILY_CAP}/day).")
    if used["by_platform"].get(adapter, 0) >= cfg.APPLY_PLATFORM_DAILY_CAP:
        name = apply_options.ADAPTER_NAMES.get(adapter, adapter)
        raise _error(429, "platform_cap",
                     f"{name} limit reached for today ({cfg.APPLY_PLATFORM_DAILY_CAP}/day).")


def _company_title(key: str) -> str:
    return key.rsplit("|", 1)[0]


def _check_duplicates(db: Session, user_id: str, job: Job, key: str, *, exclude_run: str | None = None) -> None:
    uj = job_store.get_user_job(db, user_id, job.id)
    if uj is not None and uj.state == "applied":
        raise _error(409, "already_applied", "You already applied to this job.")
    applied = (db.query(Application.id)
               .filter(Application.user_id == user_id, Application.status.in_(APPLIED_STATUSES),
                       or_(Application.job_key == key, Application.job_url == job.url)).first())
    if applied:
        raise _error(409, "already_applied", "You already applied to this job.")

    since = _naive(_now() - timedelta(days=get_settings().APPLY_COMPANY_COOLDOWN_DAYS))
    ct = _company_title(key)
    recent_app = (db.query(Application.id)
                  .filter(Application.user_id == user_id, Application.status.in_(APPLIED_STATUSES),
                          Application.job_key.like(ct.replace("%", "").replace("_", "\\_") + "|%", escape="\\"),
                          Application.updated_at >= since).first())
    q = db.query(ApplyRun.id).filter(ApplyRun.user_id == user_id, ApplyRun.company_title == ct,
                                     ApplyRun.state == "submitted", ApplyRun.submitted_at >= since)
    if exclude_run:
        q = q.filter(ApplyRun.id != exclude_run)
    if recent_app or q.first():
        days = get_settings().APPLY_COMPANY_COOLDOWN_DAYS
        raise _error(409, "company_cooldown",
                     f"You applied to this role at {job.company} in the last {days} days.")


def _check_switches(db: Session, user: User, s: ApplySettings) -> None:
    if not feature_flags.get_flag(db, "auto_apply_enabled"):
        raise _error(503, "auto_apply_off", "Auto apply is turned off by the admin.")
    if s.paused:
        raise _error(409, "apply_paused", "You paused auto apply. Turn it back on in Apply settings.")
    if not user.is_active:
        raise _error(403, "account_disabled", "Your account is disabled.")


# ---------------------------------------------------------------- plan values

def _is_eeo(p: dict) -> bool:
    return p.get("source") == "eeo" or bool(_EEO_LABEL.search(p.get("label") or ""))


def _store_value(p: dict, value: str | None) -> dict:
    """EEO answers are sealed in the plan like in the profile (P3.2): never
    plain text at rest."""
    p = {k: v for k, v in p.items() if k != "sealed"}
    if value and _is_eeo(p):
        return {**p, "value": None, "sealed": key_store.seal(value)}
    return {**p, "value": value}


def plan_value(p: dict) -> str | None:
    return key_store.unseal(p["sealed"]) if p.get("sealed") else p.get("value")


# ---------------------------------------------------------------- tracker

def _job_dict(job: Job) -> dict:
    return {"url": job.url, "title": job.title, "company": job.company, "source": job.best_source,
            "location": job.location}


def _transition(db: Session, run: ApplyRun, state: str, message: str | None = None, *,
                tracker_status: str | None = None, error: str | None = None) -> None:
    """Moves the run and writes the step to the tracker (CLAUDE.md 7.8
    status model). tracker_status "" skips the tracker write."""
    run.state, run.message = state, message
    if error is not None:
        run.error = error[:2000]
    if state not in RUN_ACTIVE_STATES:
        run.active_key = None
    db.commit()
    status = state if tracker_status is None else tracker_status
    job = db.get(Job, run.job_id)
    if job is None or not status:
        return
    row = record_status(user_id=run.user_id, job=_job_dict(job), status=status, status_source="apply_engine",
                        note=message)
    if run.application_id != row.id:
        run.application_id = row.id
        db.commit()


# ---------------------------------------------------------------- create

def create_run(db: Session, user: User, job: Job, *, mode: str) -> ApplyRun:
    """Preflight + a queued run. The caller dispatches prepare_task."""
    s = get_settings_row(db, user.id)
    _check_switches(db, user, s)
    job_dict = job_store.as_job(job)
    opts = apply_options.options(job_dict)
    adapter = opts["adapter"]
    if mode == "auto":
        if not adapter or adapter_registry.get(adapter) is None or not adapter_registry.get(adapter).can_submit:
            raise _error(409, "auto_not_supported", opts["auto"]["reason"] or "Auto apply doesn't support this job.")
        max_level = entitlements.effective(db, user.id, "auto_apply").max_level or 1
        if max_level < 2:
            raise _error(403, "level_not_allowed",
                         "Your account allows level 1 (Assist) only. Ask an admin to allow level 2.")
        if s.risk_accepted_at is None:
            raise _error(409, "risk_notice_required", "Accept the auto-apply risk notice in Apply settings first.")
        if s.level < 2:
            raise _error(409, "level_off", "Turn on level 2 (review & submit) in Apply settings first.")
        if adapter == "generic" and not s.allow_generic:
            raise _error(409, "generic_off",
                         "This job uses the AI form reader (beta). Turn it on in Apply settings first.")
        block = apply_blocks.paused(apply_blocks.platform_of(adapter, job.apply_url or job.url, job.best_source)[0])
        if block:
            raise _error(409, "platform_blocked", apply_blocks.reason(block))
        level = 2
    else:  # fast
        if adapter != "linkedin":
            raise _error(409, "fast_not_supported", opts["fast"]["reason"] or "Fast apply doesn't support this job.")
        if get_settings().ENVIRONMENT == "production":
            raise _error(409, "fast_not_supported", opts["fast"]["reason"])
        level = 1
    _check_caps(db, user.id, s, mode=mode, adapter=adapter)
    key = compute_job_key(job_dict)
    _check_duplicates(db, user.id, job, key)
    apply_prep.usable_master(db, user.id)

    url = (job.apply_url if adapter != "linkedin" and job.apply_url else job.url) or job.url
    run = ApplyRun(user_id=user.id, job_id=job.id, job_key=key[:700], company_title=_company_title(key)[:800],
                   mode=mode, level=level, adapter=adapter, apply_url=url, state="queued",
                   active_key=f"{user.id}|{key}"[:800], plan={})
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _error(409, "run_active", "This job already has an apply run in progress.")
    _transition(db, run, "queued", "Waiting for a browser on the apply worker.")
    return run


# ---------------------------------------------------------------- browser

@contextlib.contextmanager
def browser_session(*, headless: bool, profile_dir: str | None = None, keep_open: bool = False) -> Iterator:
    """A Playwright page. Auto runs use a fresh, throw-away context; Fast
    apply uses the user's own persistent profile (their LinkedIn sign-in)
    and leaves the window open for them."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = None
    try:
        if profile_dir:
            context = pw.chromium.launch_persistent_context(profile_dir, headless=headless)
        else:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1280, "height": 1600})
        page = context.new_page()
        page.set_default_timeout(get_settings().APPLY_PAGE_TIMEOUT_SECONDS * 1000)
        yield page
    finally:
        if not keep_open:
            with contextlib.suppress(Exception):
                (browser.close() if browser else context.close())
            with contextlib.suppress(Exception):
                pw.stop()


def run_isolated(fn: Callable[[], object]) -> object:
    """Playwright's sync API refuses to run inside an asyncio loop; a fresh
    thread always starts clean (same reason as browser_apply)."""
    out: dict = {}

    def target() -> None:
        try:
            out["result"] = fn()
        except BaseException as e:  # noqa: BLE001 — re-raised on the caller's thread
            out["error"] = e

    t = threading.Thread(target=target, name="apply-engine", daemon=True)
    t.start()
    t.join()
    if "error" in out:
        raise out["error"]
    return out.get("result")


def _profile_dir(user_id: str) -> str:
    base = Path(get_settings().BROWSER_PROFILE_DIR)
    path = base / re.sub(r"[^\w-]", "", user_id)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ---------------------------------------------------------------- proof

def store_proof(db: Session, run: ApplyRun, image: bytes, *, kind: str, text: str | None, url: str | None) -> ApplyProof:
    data_key = Fernet.generate_key()
    proof = ApplyProof(run_id=run.id, user_id=run.user_id, kind=kind, confirmation_text=(text or "")[:1000] or None,
                       page_url=(url or "")[:2000] or None,
                       wrapped_key=master_fernet().encrypt(data_key).decode(),
                       ciphertext=Fernet(data_key).encrypt(image).decode())
    db.add(proof)
    db.commit()
    return proof


def load_proof(proof: ApplyProof) -> bytes:
    data_key = master_fernet().decrypt(proof.wrapped_key.encode())
    return Fernet(data_key).decrypt(proof.ciphertext.encode())


def purge_old_proofs(db: Session) -> int:
    cutoff = _naive(_now() - timedelta(days=get_settings().APPLY_PROOF_RETENTION_DAYS))
    n = db.query(ApplyProof).filter(ApplyProof.created_at < cutoff).delete()
    db.commit()
    return n


def _snap(db: Session, run: ApplyRun, adapter, page, *, kind: str, text: str | None = None) -> None:
    try:
        store_proof(db, run, adapter.screenshot(page), kind=kind, text=text, url=adapter.page_url(page))
    except Exception:  # noqa: BLE001 — a missing screenshot must not hide the real outcome
        db.rollback()
        logger.exception("could not store a screenshot for run=%s", run.id)


# ---------------------------------------------------------------- files

def _documents(db: Session, run: ApplyRun, workdir: Path) -> dict[str, str]:
    """Writes the resume (tailored when the prep pack has one) and the cover
    letter to `workdir`. Returns {"resume": path, "cover_letter": path}."""
    job = db.get(Job, run.job_id)
    pack, build = apply_prep.request_pack(db, user_id=run.user_id, job=job, tailor_resume=True, cover_letter=True,
                                          regenerate=False)
    if build or pack.status == "pending":
        apply_prep.build_pack(db, pack)
    db.commit()
    run.pack_id = pack.id
    resume = db.get(Resume, pack.tailored_resume_id) if pack.tailored_resume_id else None
    resume = resume or resume_store.master(db, run.user_id)
    run.resume_id = resume.id
    out: dict[str, str] = {}
    resume_path = workdir / resume.file_name
    resume_path.write_bytes(resume_store.load_file(db, resume))
    out["resume"] = str(resume_path)
    if pack.cover_letter:
        letter = workdir / "Cover letter.txt"
        letter.write_text(pack.cover_letter, encoding="utf-8")
        out["cover_letter"] = str(letter)
    db.commit()
    return out


def _file_plan(fields: list[dict], docs: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """{field key: "resume" | "cover_letter"} and labels of required file fields we can't fill."""
    plan, missing = {}, []
    file_fields = [f for f in fields if f["type"] == "file"]
    for f in file_fields:
        label = f["label"] or f["key"]
        is_cover = bool(_COVER_LABEL.search(label)) or "cover" in f["key"].lower()
        is_resume = not is_cover and (bool(_RESUME_LABEL.search(label)) or "resume" in f["key"].lower()
                                      or len(file_fields) == 1)
        if is_cover and "cover_letter" in docs:
            plan[f["key"]] = "cover_letter"
        elif is_resume and "resume" not in plan.values():
            plan[f["key"]] = "resume"
        elif f["required"]:
            missing.append(label)
    return plan, missing


# ---------------------------------------------------------------- plan

def _plan_fields(db: Session, run: ApplyRun, job: Job, fields: list[dict], docs: dict[str, str],
                 known: dict[str, dict] | None = None) -> tuple[list[dict], dict[str, str], list[str]]:
    """(plan fields, file plan, required files we can't provide) for the controls on one page. `known`
    keeps answers from an earlier pass of the same run (a multi-page form read again after the user
    answered a question)."""
    known = known or {}
    fresh = [f for f in fields if f["key"] not in known]
    answers = {a["id"]: a for a in answer_bank.answer_fields(
        db, run.user_id, _answerable(fresh), job=job_store.as_job(job))} if fresh else {}
    db.commit()
    file_plan, missing_files = _file_plan(fields, docs)
    plan_fields = []
    for f in fields:
        if f["type"] == "file":
            continue
        if f["key"] in known:
            plan_fields.append({**known[f["key"]], "label": f["label"], "type": f["type"],
                                "options": f.get("options") or [], "required": f["required"]})
            continue
        a = answers.get(f["key"], {})
        plan_fields.append(_store_value(
            {"key": f["key"], "label": f["label"], "type": f["type"], "options": f.get("options") or [],
             "required": f["required"], "source": a.get("source", "none"),
             "approved": a.get("approved", False),
             "needs_input": bool(a.get("needs_input")) and f["required"]}, a.get("value")))
    return plan_fields, file_plan, missing_files


def _run_tools(db: Session, run: ApplyRun) -> RunTools:
    """The AI form reader's two AI calls for this run (P4c): form_map (text) and form_vision (screenshot),
    both through the router, ledgered with the run id."""
    cfg = get_settings()

    def map_page(prompt: str) -> str | None:
        try:
            return ai_router.complete(db, user_id=run.user_id, task="form_map", system=MAP_SYSTEM,
                                      user_prompt=prompt, json_mode=True, max_tokens=300, ref=run.id,
                                      validate=valid_map).text
        except ai_router.NoModelAvailable as e:
            logger.info("form_map unavailable for run=%s: %s", run.id, e)
            return None

    def look(png: bytes, prompt: str) -> tuple[str, float]:
        outcome = ai_router.StreamOutcome()
        for _ in ai_router.stream(db, user_id=run.user_id, task="form_vision", system=vision.SYSTEM,
                                  user_prompt=prompt, outcome=outcome, max_tokens=300, ref=run.id,
                                  images=[("image/png", png)]):
            pass
        return outcome.text, outcome.cost_usd

    used = (run.plan or {}).get("vision") or {}
    return RunTools(map_page=map_page, look=look, max_vision_steps=cfg.GENERIC_APPLY_VISION_MAX_STEPS,
                    max_vision_usd=cfg.GENERIC_APPLY_VISION_MAX_USD, vision_steps=int(used.get("steps") or 0),
                    vision_cost=float(used.get("cost_usd") or 0.0), map_calls=int(used.get("map_calls") or 0))


def _save_usage(db: Session, run: ApplyRun, adapter) -> None:
    tools = getattr(adapter, "tools", None)
    if isinstance(tools, RunTools):
        run.plan = {**(run.plan or {}), "vision": tools.usage()}
        db.commit()


# ---------------------------------------------------------------- prepare (worker)

def _get_active(db: Session, run_id: str, expected: tuple[str, ...]) -> ApplyRun | None:
    run = db.get(ApplyRun, run_id)
    if run is None or run.state not in expected:
        return None
    return run


def _still_allowed(db: Session, run: ApplyRun) -> bool:
    user = db.get(User, run.user_id)
    s = get_settings_row(db, run.user_id)
    try:
        _check_switches(db, user, s)
    except HTTPException as e:
        _transition(db, run, "cancelled", e.detail["message"], tracker_status="cancelled")
        return False
    return True


def prepare(db: Session, run_id: str) -> ApplyRun | None:
    """Opens the form, answers every field, and stops at awaiting_input /
    awaiting_review (auto) or leaves a filled Easy Apply step open (fast)."""
    run = _get_active(db, run_id, ("queued",))
    if run is None or not _still_allowed(db, run):
        return run
    _transition(db, run, "preparing", "Reading the application form.", tracker_status="preparing")
    adapter = adapter_registry.get(run.adapter).for_run(_run_tools(db, run))
    job = db.get(Job, run.job_id)
    cfg = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="jf-apply-"))
    fast = run.mode == "fast"
    try:
        docs = _documents(db, run, workdir)
        with browser_session(headless=cfg.APPLY_HEADLESS and not fast,
                             profile_dir=_profile_dir(run.user_id) if fast else None, keep_open=fast) as page:
            try:
                adapter.open(page, run.apply_url, cfg.APPLY_PAGE_TIMEOUT_SECONDS * 1000)
                apply_blocks.note_ok(_platform(run, job)[0])
                if adapter.multi_step:
                    try:
                        return _prepare_steps(db, run, adapter, page, job, docs)
                    finally:
                        _save_usage(db, run, adapter)
                fields = adapter.read_form(page)
            except HardStop as stop:
                _save_usage(db, run, adapter)
                if isinstance(stop, Blocked):
                    return _blocked_stop(db, run, adapter, page, job, stop)
                _snap(db, run, adapter, page, kind="stopped", text=stop.reason)
                _transition(db, run, "needs_user", stop.reason + " Finish this application on the job page.",
                            tracker_status="needs_user")
                return run
            if not fields:
                _snap(db, run, adapter, page, kind="stopped")
                _transition(db, run, "needs_user", "No application form was found on the page.",
                            tracker_status="needs_user")
                return run

            plan_fields, file_plan, missing_files = _plan_fields(db, run, job, fields, docs)
            run.plan = {"fields": plan_fields, "files": file_plan}
            db.commit()

            human = None if fast else adapter.needs_human(page)
            if human:
                _snap(db, run, adapter, page, kind="stopped", text=human)
                _transition(db, run, "needs_user", human + " Your answers are ready below — copy them into the "
                                                          "form on the job page.", tracker_status="needs_user")
                return run
            if missing_files:
                _snap(db, run, adapter, page, kind="stopped")
                _transition(db, run, "needs_user",
                            "The form asks for a file the app can't provide: " + ", ".join(missing_files[:3]),
                            tracker_status="needs_user")
                return run

            if fast:
                values = {p["key"]: plan_value(p) for p in plan_fields if plan_value(p) not in (None, "")}
                files = {k: docs[v] for k, v in file_plan.items() if v in docs}
                adapter.fill_form(page, fields, values, files)
                _transition(db, run, "needs_user",
                            "Check the answers in the LinkedIn window and click Submit yourself, then mark it done.",
                            tracker_status="prefilled")
                return run
    except HardStop as stop:
        _transition(db, run, "needs_user", stop.reason, tracker_status="needs_user")
        return run
    except Exception as e:  # noqa: BLE001 — an adapter bug must not leave the run "preparing"
        logger.exception("prepare failed for run=%s", run.id)
        db.rollback()
        run = db.get(ApplyRun, run_id)
        _transition(db, run, "failed", "Something went wrong while reading the form.", tracker_status="failed",
                    error=f"{type(e).__name__}: {e}")
        return run
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # uploads read the files when they are set

    _after_answers(db, run)
    return run


def _platform(run: ApplyRun, job: Job | None) -> tuple[str, str]:
    return apply_blocks.platform_of(run.adapter, run.apply_url, job.best_source if job else None)


def _blocked_stop(db: Session, run: ApplyRun, adapter, page, job: Job | None, stop: Blocked) -> ApplyRun:
    """The site refused the server's browser (QA5): hand the run over, say so plainly and pause Auto apply on
    that platform for everyone (the block is on the server's IP)."""
    key, name = _platform(run, job)
    row = apply_blocks.record(key, name, run.id)
    logger.warning("apply run=%s blocked by %s: %s (paused %s h)", run.id, name, stop.reason, row["hours"])
    _snap(db, run, adapter, page, kind="stopped", text=stop.reason)
    _transition(db, run, "needs_user",
                f"{name} blocked the app's browser — it treats our server as a bot. Apply on the job page in your "
                f"own browser. Auto apply on {name} is paused for {row['hours']} h.",
                tracker_status="needs_user", error=f"Blocked: {stop.reason}")
    return run


def _prepare_steps(db: Session, run: ApplyRun, adapter, page, job: Job, docs: dict[str, str]) -> ApplyRun:
    """The AI form reader's prepare (P4c §3.2): answer each page of the form, fill it, go to the next one, and
    stop before the final Submit. A required question nobody can answer stops the walk at awaiting_input;
    answering it queues prepare again, which reads the form from the start with the answers kept."""
    cfg = get_settings()

    def stop(message: str) -> ApplyRun:
        _snap(db, run, adapter, page, kind="stopped", text=message)
        _transition(db, run, "needs_user", message, tracker_status="needs_user")
        return run

    known = {p["key"]: p for p in (run.plan or {}).get("fields", []) if p.get("source") == "user" or plan_value(p)}
    steps: list[dict] = []
    all_fields: list[dict] = []
    all_files: dict[str, str] = {}
    for n in range(1, cfg.GENERIC_APPLY_STEP_LIMIT + 1):
        fields = adapter.read_form(page)
        if not fields:
            return stop("No application form was found on the page." if n == 1 else
                        f"Page {n} of the form has no questions the app can read. Finish it on the job page.")
        human = adapter.needs_human(page)
        if human:
            return stop(human + " Your answers so far are ready below — copy them into the form on the job page.")
        plan_fields, file_plan, missing_files = _plan_fields(db, run, job, fields, docs, known)
        seen = {p["key"] for p in all_fields}
        all_fields += [p for p in plan_fields if p["key"] not in seen]
        all_files.update(file_plan)
        steps.append({"page": n, "url": adapter.page_url(page)[:500], "keys": [p["key"] for p in plan_fields],
                      "files": file_plan})
        run.plan = {**(run.plan or {}), "fields": all_fields, "files": all_files, "steps": steps, "complete": False}
        db.commit()
        _snap(db, run, adapter, page, kind="step", text=f"Page {n}")
        if missing_files:
            return stop("The form asks for a file the app can't provide: " + ", ".join(missing_files[:3]))
        if adapter.is_final(page):
            run.plan = {**run.plan, "complete": True}
            db.commit()
            _after_answers(db, run)
            return run
        if any(p["needs_input"] for p in plan_fields):
            _after_answers(db, run)  # awaiting_input; the next pages are read after the user answers
            return run
        values = {p["key"]: plan_value(p) for p in plan_fields if plan_value(p) not in (None, "")}
        files = {k: docs[v] for k, v in file_plan.items() if v in docs}
        problems = adapter.fill_form(page, fields, values, files)
        if problems:
            return stop(f"Could not fill page {n} of the form: " + "; ".join(problems[:3]))
        adapter.next_step(page)
    return stop(f"This form has more than {cfg.GENERIC_APPLY_STEP_LIMIT} pages — finish it on the job page.")


def _answerable(fields: list[dict]) -> list[dict]:
    from app.apply.forms import for_answering
    return for_answering(fields)


def _after_answers(db: Session, run: ApplyRun) -> None:
    missing = [p["label"] for p in run.plan.get("fields", []) if p["needs_input"]]
    if missing:
        _transition(db, run, "awaiting_input",
                    f"{len(missing)} required question(s) need your answer: " + "; ".join(m[:60] for m in missing[:5]),
                    tracker_status="awaiting_input")
    elif run.plan.get("steps") and not run.plan.get("complete"):
        # A multi-page form stopped at a question: read the rest now (the route queues prepare again).
        _transition(db, run, "queued", "Your answers are saved — reading the next page of the form.",
                    tracker_status="")
    else:
        drafts = sum(1 for p in run.plan.get("fields", []) if p["source"] == "ai" and plan_value(p))
        note = f" {drafts} answer(s) were drafted by AI — check them." if drafts else ""
        if run.adapter == "generic" and not get_settings().GENERIC_APPLY_SUBMIT:
            note += (" The AI form reader is in test mode: check the answers here, then submit them yourself "
                     "on the job page.")
        _transition(db, run, "awaiting_review", "Ready for your review." + note, tracker_status="awaiting_review")


# ---------------------------------------------------------------- user actions

def answer(db: Session, run: ApplyRun, values: dict[str, str]) -> ApplyRun:
    if run.state not in ("awaiting_input", "awaiting_review"):
        raise _error(409, "run_not_waiting", "This run is not waiting for answers.")
    fields = {p["key"]: p for p in run.plan.get("fields", [])}
    unknown = [k for k in values if k not in fields]
    if unknown:
        raise _error(422, "unknown_field", "Unknown field(s): " + ", ".join(unknown[:5]))
    plan_fields = []
    for p in run.plan.get("fields", []):
        p = dict(p)
        if p["key"] in values:
            value = (values[p["key"]] or "").strip()
            if p["options"] and value and value not in p["options"]:
                raise _error(422, "bad_option", f"'{p['label'][:60]}' must be one of: " + ", ".join(p["options"][:10]))
            p.update(source="user", approved=True, needs_input=p["required"] and not value)
            eeo = _is_eeo(p)
            p = _store_value(p, value or None)
            if value and not eeo:  # EEO answers stay out of the plain-text bank
                answer_bank.save_user_answer(db, run.user_id, question=p["label"][:500], answer=value,
                                             field_type="select" if p["options"] else "text")
        plan_fields.append(p)
    run.plan = {**run.plan, "fields": plan_fields}
    db.commit()
    _after_answers(db, run)
    return run


def approve(db: Session, user: User, run: ApplyRun) -> tuple[ApplyRun, int]:
    """Level 2: the user approves this run's answers; it is submitted after
    a pacing gap. Returns (run, countdown seconds)."""
    if run.state != "awaiting_review":
        raise _error(409, "run_not_ready", "This run is not waiting for your review.")
    if run.adapter == "generic" and not get_settings().GENERIC_APPLY_SUBMIT:
        raise _error(409, "generic_test_mode", "The AI form reader is in test mode — submit this application "
                                               "yourself on the job page, then mark it done.")
    s = get_settings_row(db, user.id)
    _check_switches(db, user, s)
    if not within_hours(s):
        raise _error(409, "outside_hours",
                     f"Auto apply submits only between {s.hours_start:02d}:00 and {s.hours_end:02d}:00 "
                     f"({s.timezone}). Approve it again then.")
    _check_caps(db, user.id, s, mode="auto", adapter=run.adapter)
    job = db.get(Job, run.job_id)
    _check_duplicates(db, user.id, job, run.job_key, exclude_run=run.id)
    # Reviewing the run approves the AI drafts it used.
    drafted = [p["label"] for p in run.plan.get("fields", []) if p["source"] == "ai" and plan_value(p)]
    if drafted:
        hashes = [answer_bank.question_hash(q) for q in drafted]
        (db.query(AnswerBankEntry).filter(AnswerBankEntry.user_id == user.id,
                                          AnswerBankEntry.question_hash.in_(hashes))
         .update({"approved": True}, synchronize_session=False))
    run.approved_at = _now()
    countdown = _pacing(user.id, run.adapter)
    db.commit()
    _transition(db, run, "submitting", "Approved — submitting shortly.", tracker_status="submitting")
    return run, countdown


def _pacing(user_id: str, adapter: str) -> int:
    """Seconds to wait so two submits on one platform are 30-90 s apart."""
    cfg = get_settings()
    r = get_redis()
    key = f"apply_next_slot:{user_id}:{adapter}"
    now = time.time()
    next_free = float(r.get(key) or 0)
    start = max(now, next_free)
    r.set(key, start + random.uniform(cfg.APPLY_MIN_GAP_SECONDS, cfg.APPLY_MAX_GAP_SECONDS), ex=3600)
    return int(start - now)


def cancel(db: Session, run: ApplyRun, reason: str = "Cancelled by you.") -> ApplyRun:
    if run.state not in RUN_ACTIVE_STATES:
        raise _error(409, "run_finished", "This run has already finished.")
    _transition(db, run, "cancelled", reason, tracker_status="cancelled")
    return run


def mark_done(db: Session, run: ApplyRun, *, submitted: bool) -> ApplyRun:
    """The user finished a needs_user / Fast apply run on the real page."""
    if run.state not in ("needs_user", "awaiting_input", "awaiting_review"):
        raise _error(409, "run_not_waiting", "This run is not waiting for you.")
    if not submitted:
        _transition(db, run, "cancelled", "You didn't submit this application.", tracker_status="cancelled")
        return run
    run.submitted_at = _now()
    _transition(db, run, "done", "You submitted this application yourself.", tracker_status="")
    job = db.get(Job, run.job_id)
    row = record_status(user_id=run.user_id, job=_job_dict(job), status="submitted", status_source="manual",
                        note="submitted by the user after an apply run")
    run.application_id = row.id
    job_store.set_state(db, user_id=run.user_id, job_id=run.job_id, state="applied")
    db.commit()
    return run


def cancel_active_runs(db: Session, user_id: str, *, reason: str) -> int:
    runs = db.query(ApplyRun).filter(ApplyRun.user_id == user_id, ApplyRun.state.in_(RUN_ACTIVE_STATES)).all()
    for run in runs:
        _transition(db, run, "cancelled", reason, tracker_status="cancelled")
    return len(runs)


# ---------------------------------------------------------------- submit (worker)

def submit(db: Session, run_id: str) -> ApplyRun | None:
    run = _get_active(db, run_id, ("submitting",))
    if run is None or not _still_allowed(db, run):
        return run
    adapter = adapter_registry.get(run.adapter)
    if adapter is None or not adapter.can_submit:
        _transition(db, run, "needs_user", "This platform is never submitted automatically.",
                    tracker_status="needs_user")
        return run
    if run.adapter == "generic" and not get_settings().GENERIC_APPLY_SUBMIT:
        _transition(db, run, "needs_user", "The AI form reader is in test mode — submit this application yourself "
                                           "on the job page.", tracker_status="needs_user")
        return run
    adapter = adapter.for_run(_run_tools(db, run))
    s = get_settings_row(db, run.user_id)
    try:
        _check_caps(db, run.user_id, s, mode="auto", adapter=run.adapter)
    except HTTPException as e:
        _transition(db, run, "awaiting_review", e.detail["message"] + " Approve it again tomorrow.",
                    tracker_status="awaiting_review")
        return run

    cfg = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="jf-apply-"))
    try:
        docs = _documents(db, run, workdir)
        with browser_session(headless=cfg.APPLY_HEADLESS) as page:
            try:
                adapter.open(page, run.apply_url, cfg.APPLY_PAGE_TIMEOUT_SECONDS * 1000)
                if adapter.multi_step:
                    try:
                        return _submit_steps(db, run, adapter, page, docs)
                    finally:
                        _save_usage(db, run, adapter)
                fields = adapter.read_form(page)
            except HardStop as stop:
                if isinstance(stop, Blocked):
                    return _blocked_stop(db, run, adapter, page, db.get(Job, run.job_id), stop)
                _snap(db, run, adapter, page, kind="stopped", text=stop.reason)
                _transition(db, run, "needs_user", stop.reason + " Finish this application on the job page.",
                            tracker_status="needs_user")
                return run
            human = adapter.needs_human(page)
            if human:
                _snap(db, run, adapter, page, kind="stopped", text=human)
                _transition(db, run, "needs_user", human + " Finish this application on the job page.",
                            tracker_status="needs_user")
                return run
            plan = run.plan or {}
            values = {p["key"]: plan_value(p) for p in plan.get("fields", []) if plan_value(p) not in (None, "")}
            files = {k: docs[v] for k, v in (plan.get("files") or {}).items() if v in docs}
            on_page = {f["key"] for f in fields}
            new_required = [f["label"] or f["key"] for f in fields
                            if f["required"] and f["type"] != "file"
                            and f["key"] not in {p["key"] for p in plan.get("fields", [])}]
            if new_required:
                _snap(db, run, adapter, page, kind="stopped")
                _transition(db, run, "needs_user",
                            "The form changed since you reviewed it (new required questions: "
                            + "; ".join(x[:60] for x in new_required[:3]) + ").", tracker_status="needs_user")
                return run
            problems = adapter.fill_form(page, fields, {k: v for k, v in values.items() if k in on_page}, files)
            filled = set(values) | set(files)
            empty = [f["label"] or f["key"] for f in fields if f["required"] and f["key"] not in filled]
            if problems or empty:
                _snap(db, run, adapter, page, kind="stopped")
                detail = "; ".join((problems + [f"'{e[:60]}' is empty" for e in empty])[:4])
                _transition(db, run, "needs_user", "Could not fill the form: " + detail, tracker_status="needs_user")
                return run

            return _finish_submit(db, run, adapter, page)
    except Exception as e:  # noqa: BLE001
        logger.exception("submit failed for run=%s", run.id)
        db.rollback()
        run = db.get(ApplyRun, run_id)
        _transition(db, run, "needs_user", "Something went wrong while submitting — check the job page before "
                                           "trying again.", tracker_status="needs_user", error=f"{type(e).__name__}: {e}")
        return run
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _finish_submit(db: Session, run: ApplyRun, adapter, page) -> ApplyRun:
    result = adapter.submit_and_wait(page, wait_ms=15000)
    if result.state == "submitted":
        _snap(db, run, adapter, page, kind="confirmation", text=result.text)
        run.submitted_at = _now()
        _transition(db, run, "submitted", "Submitted. " + (result.text[:200] if result.text else ""),
                    tracker_status="submitted")
        job_store.set_state(db, user_id=run.user_id, job_id=run.job_id, state="applied")
        db.commit()
        return run
    _snap(db, run, adapter, page, kind="stopped", text=result.text)
    reasons = {"captcha": "A captcha appeared — finish and submit this application yourself.",
               "invalid": "The form reported an error: " + result.text,
               "unknown": "We couldn't confirm the application went through — check the job page."}
    _transition(db, run, "needs_user", reasons.get(result.state, result.text), tracker_status="needs_user")
    return run


def _submit_steps(db: Session, run: ApplyRun, adapter, page, docs: dict[str, str]) -> ApplyRun:
    """Replays the reviewed pages of a multi-page form (P4c): each page is read again and must still match
    the plan; the last one is submitted."""
    def stop(message: str) -> ApplyRun:
        _snap(db, run, adapter, page, kind="stopped", text=message)
        _transition(db, run, "needs_user", message, tracker_status="needs_user")
        return run

    plan = run.plan or {}
    steps = plan.get("steps") or []
    by_key = {p["key"]: p for p in plan.get("fields", [])}
    if not steps or not plan.get("complete"):
        return stop("This run was never fully prepared — finish it on the job page.")
    for i, step in enumerate(steps):
        fields = adapter.read_form(page)
        human = adapter.needs_human(page)
        if human:
            return stop(human + " Finish this application on the job page.")
        on_page = {f["key"] for f in fields}
        new_required = [f["label"] or f["key"] for f in fields
                        if f["required"] and f["type"] != "file" and f["key"] not in by_key]
        if new_required:
            return stop("The form changed since you reviewed it (new required questions: "
                        + "; ".join(x[:60] for x in new_required[:3]) + ").")
        values = {k: plan_value(by_key[k]) for k in step["keys"]
                  if k in on_page and k in by_key and plan_value(by_key[k]) not in (None, "")}
        files = {k: docs[v] for k, v in (step.get("files") or {}).items() if v in docs and k in on_page}
        problems = adapter.fill_form(page, fields, values, files)
        empty = [f["label"] or f["key"] for f in fields
                 if f["required"] and f["key"] not in set(values) | set(files)]
        if problems or empty:
            detail = "; ".join((problems + [f"'{e[:60]}' is empty" for e in empty])[:4])
            return stop(f"Could not fill page {i + 1} of the form: " + detail)
        if i < len(steps) - 1:
            adapter.next_step(page)
            continue
        if not adapter.is_final(page):
            return stop("The last page of the form has no Submit button the app can find.")
        return _finish_submit(db, run, adapter, page)
    return run


def proofs(db: Session, run: ApplyRun) -> list[ApplyProof]:
    return db.query(ApplyProof).filter(ApplyProof.run_id == run.id).order_by(ApplyProof.created_at).all()


def get_owned(db: Session, user_id: str, run_id: str) -> ApplyRun:
    run = db.query(ApplyRun).filter(ApplyRun.id == run_id, ApplyRun.user_id == user_id).first()
    if run is None:
        raise _error(404, "run_not_found", "No apply run with this id.")
    return run

