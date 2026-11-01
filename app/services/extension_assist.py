"""
app/services/extension_assist.py
----------------------------------
Fast apply in the user's own browser (plan docs/plans/P8b-fast-apply-extension.md, E3a).

The web app starts a Fast apply run (apply_engine.create_run → state `in_browser`, plan.runner
"extension") and opens the LinkedIn job in a new tab. There the extension, signed in as the user's own
LinkedIn, opens the Easy Apply dialog and asks this module for the answers to each page's fields:

  run_for_page   the user's in_browser run for the LinkedIn job on this page (matched by LinkedIn job id)
  fill           answers for one page: profile → EEO → answer bank → AI (answer_bank.answer_fields, through
                 apply_engine._plan_fields, so the run's review screen shows them like any other run)
  remember       a question the user answered in the extension's panel → the plan + the answer bank
  resume_file    the tailored resume from the prep pack when it is ready, else the master
  finish         LinkedIn confirmed ("application was sent") → tracker `submitted` (status_source
                 "extension"); the tab closed first → needs_user, "did you submit it?"

The extension fills; the user clicks Next and Submit. Nothing here submits anything or touches LinkedIn.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.apply import ApplyPack, ApplyRun
from app.models.jobs import Job
from app.models.profile import Resume
from app.services import answer_bank, apply_engine, entitlements, extension, resume_store

MAX_FIELDS = 60
FIELD_TYPES = {"text", "textarea", "number", "select", "radio", "checkbox", "combobox", "file"}
_VIEW_ID = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})(?:[/?#]|$)")
_LINKEDIN_HOSTS = ("linkedin.com",)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, "details": {}})


def assist_user(db: Session, raw_token: str | None):
    """The token's user, when Fast apply in the browser is allowed for them."""
    user, _ = extension.user_for_token(db, raw_token)
    if not entitlements.effective(db, user.id, "auto_apply").enabled:
        raise _error(403, "feature_disabled", "Apply features are not enabled for this account.")
    if not apply_engine.get_settings_row(db, user.id).extension_assist:
        db.commit()
        raise _error(403, "assist_off", "Fast apply in your browser is off. Turn it on in Job Finder › Apply settings.")
    return user


def linkedin_job_id(url: str) -> str | None:
    """LinkedIn's numeric job id from a job page (/jobs/view/123…, /jobs/view/title-at-co-123…) or a search
    page showing one job (?currentJobId=123…)."""
    try:
        parts = urlparse(url or "")
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _LINKEDIN_HOSTS):
        return None
    m = _VIEW_ID.search(parts.path + ("/" if not parts.path.endswith("/") else ""))
    if m:
        return m.group(1)
    current = parse_qs(parts.query).get("currentJobId", [""])[0]
    return current if current.isdigit() and len(current) >= 6 else None


def _browser_runs(db: Session, user_id: str) -> list[ApplyRun]:
    rows = (db.query(ApplyRun).filter(ApplyRun.user_id == user_id, ApplyRun.state == "in_browser")
            .order_by(ApplyRun.created_at.desc()).limit(20).all())
    return [r for r in rows if (r.plan or {}).get("runner") == "extension"]


def run_for_page(db: Session, user_id: str, page_url: str) -> ApplyRun | None:
    wanted = linkedin_job_id(page_url)
    if not wanted:
        return None
    return next((r for r in _browser_runs(db, user_id) if linkedin_job_id(r.apply_url) == wanted), None)


def get_run(db: Session, user_id: str, run_id: str) -> ApplyRun:
    run = db.query(ApplyRun).filter(ApplyRun.id == run_id, ApplyRun.user_id == user_id).first()
    if run is None or (run.plan or {}).get("runner") != "extension":
        raise _error(404, "run_not_found", "No Fast apply run with this id.")
    if run.state != "in_browser":
        raise _error(409, "run_not_in_browser", "This Fast apply run has already finished.")
    return run


def _clean_fields(fields: list[dict]) -> list[dict]:
    if len(fields) > MAX_FIELDS:
        raise _error(422, "too_many_fields", f"At most {MAX_FIELDS} fields per page.")
    out, seen = [], set()
    for f in fields:
        key = str(f.get("key") or "").strip()[:200]
        ftype = str(f.get("type") or "text")
        if not key or key in seen or ftype not in FIELD_TYPES:
            continue
        seen.add(key)
        out.append({"key": key, "label": str(f.get("label") or "").strip()[:500], "type": ftype,
                    "options": [str(o).strip()[:200] for o in (f.get("options") or [])[:200] if str(o).strip()],
                    "required": bool(f.get("required"))})
    return out


def fill(db: Session, run: ApplyRun, fields: list[dict]) -> dict:
    """Answers for the fields on one page. Fields already answered in this run keep their answer (the user
    went Back, or the page was read twice)."""
    fields = _clean_fields(fields)
    job = db.get(Job, run.job_id)
    plan = dict(run.plan or {})
    known = {p["key"]: p for p in plan.get("fields", [])}
    page_fields, file_plan, _missing = apply_engine._plan_fields(db, run, job, fields, {"resume": "resume"}, known)
    merged = {**known, **{p["key"]: p for p in page_fields}}
    run.plan = {**plan, "fields": list(merged.values()), "files": {**(plan.get("files") or {}), **file_plan}}
    db.commit()
    answers = [{"key": p["key"], "value": apply_engine.plan_value(p), "source": p["source"],
                "needs_input": bool(p["needs_input"])} for p in page_fields]
    return {"answers": answers, "files": file_plan}


def remember(db: Session, run: ApplyRun, key: str, value: str) -> dict:
    """The user answered a question in the extension's panel."""
    value = (value or "").strip()[:2000]
    fields = list((run.plan or {}).get("fields", []))
    for i, p in enumerate(fields):
        if p["key"] != key:
            continue
        if p["options"] and value and value not in p["options"]:
            raise _error(422, "bad_option", f"'{p['label'][:60]}' must be one of: " + ", ".join(p["options"][:10]))
        p = {**p, "source": "user", "approved": True, "needs_input": p["required"] and not value}
        eeo = apply_engine._is_eeo(p)
        fields[i] = apply_engine._store_value(p, value or None)
        if value and not eeo:  # EEO answers stay out of the plain-text bank
            answer_bank.save_user_answer(db, run.user_id, question=p["label"][:500], answer=value,
                                         field_type="select" if p["options"] else "text")
        run.plan = {**run.plan, "fields": fields}
        db.commit()
        return {"key": key, "value": value, "source": "user", "needs_input": fields[i]["needs_input"]}
    raise _error(422, "unknown_field", "That question isn't part of this application.")


def resume_file(db: Session, run: ApplyRun) -> tuple[bytes, str, str]:
    """(bytes, file name, content type): the tailored resume when this job's prep pack is ready, else the
    master. Never builds a pack here (that takes a minute); the app's prep dialog does."""
    pack = (db.query(ApplyPack).filter(ApplyPack.user_id == run.user_id, ApplyPack.job_id == run.job_id,
                                       ApplyPack.status == "ready").first())
    resume = db.get(Resume, pack.tailored_resume_id) if pack and pack.tailored_resume_id else None
    resume = resume or resume_store.master(db, run.user_id)
    if resume is None:
        raise _error(409, "no_resume", "Upload a resume in Job Finder first.")
    run.resume_id = resume.id
    db.commit()
    return resume_store.load_file(db, resume), resume.file_name, resume.content_type


def finish(db: Session, run: ApplyRun, outcome: str) -> ApplyRun:
    if outcome == "submitted":
        return apply_engine.mark_done(db, run, submitted=True, source="extension")
    return apply_engine.browser_closed(db, run)
