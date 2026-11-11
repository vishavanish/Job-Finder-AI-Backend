"""
app/api/routes/apply.py
-------------------------
Apply Center (CLAUDE.md 7.7).

  - POST /apply                 deprecated: open + prefill LinkedIn Easy Apply on the worker
  - /apply/settings, /apply/runs  the apply engine (P4b) — see the routes at the end of this file
  - GET  /apply/options/{job}   which of the four apply buttons work for a job, and why not
  - POST /apply/prep            build (or reuse) the prep pack for a job: tailored resume,
                                cover letter, email draft. 202 while a worker builds it.
  - GET  /apply/prep/{pack_id}  poll a pack

The tailored resume is a normal Resume (a child of the master), so it
downloads with GET /resumes/{id}/file.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.services.entitlements import require_feature
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.apply import ApplyPack
from app.models.apply_schemas import ApplyOptions, PrepIn, PrepPackOut
from app.models.jobs import Job
from app.models.schemas import ApplyRequest, TaskResponse
from app.core.celery_tasks import apply_task
from app.services import apply_options, apply_prep, job_store
from app.services.task_params import build_apply_params

router = APIRouter(prefix="/apply", tags=["apply"])
logger = logging.getLogger("job_finder_api.apply")
settings = get_settings()


@router.post("", response_model=TaskResponse, status_code=202, deprecated=True)
@limiter.limit(settings.RATE_LIMIT_APPLY)
async def start_apply(request: Request, req: ApplyRequest, user=Depends(require_feature("auto_apply"))):
    """Deprecated (P4b): use POST /apply/runs. Kept for one release.

    Opens top-matched jobs in a real Playwright browser on the Celery
    worker and pre-fills LinkedIn Easy Apply forms — never auto-submits.
    Poll GET /tasks/{task_id} for status, and GET /applications for the
    tracked application rows (scoped to your account).

    NOTE: a client-supplied browser_profile_dir is ignored — see
    build_apply_params.
    """
    if req.browser_profile_dir:
        logger.warning("ignored client-supplied browser_profile_dir override")
    jobs = [j.model_dump() for j in req.jobs]
    response = dispatch(
        apply_task, args=[jobs, build_apply_params(req, user_id=user.id)],
        queue=settings.apply_queue, user_id=user.id,
    )
    logger.info("[task=%s] dispatched to '%s' for user=%s", response.task_id, settings.apply_queue, user.id)
    return response


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "job_not_found", "message": "No job with this id."})
    return job


def _pack_out(pack: ApplyPack) -> PrepPackOut:
    return PrepPackOut(
        id=pack.id, job_id=pack.job_id, status=pack.status, tailored_resume_id=pack.tailored_resume_id,
        cover_letter=pack.cover_letter, email_to=pack.email_to, email_subject=pack.email_subject,
        email_body=pack.email_body, mailto=apply_prep.mailto(pack), dropped=list(pack.dropped or []),
        error=pack.error, error_detail=pack.error_detail, updated_at=pack.updated_at,
        steps=apply_prep.steps(pack.id), typical_seconds=apply_prep.typical_seconds(),
    )


@router.get("/options/{job_id}", response_model=ApplyOptions)
def get_apply_options(job_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return apply_options.options(job_store.as_job(_job_or_404(db, job_id)))


@router.post("/prep", response_model=PrepPackOut, responses={202: {"model": PrepPackOut}})
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLY_PREP)
def request_prep(request: Request, body: PrepIn, response: Response,
                 user=Depends(require_feature("resume_tailoring")), db: Session = Depends(get_db)):
    """Returns the ready pack (200) or starts building one (202; poll
    GET /apply/prep/{id}). A ready pack is reused until your master resume
    changes or `regenerate` is true. Asking for nothing (both false) still
    gives the email draft."""
    job = _job_or_404(db, body.job_id)
    pack, build = apply_prep.request_pack(db, user_id=user.id, job=job, tailor_resume=body.tailor_resume,
                                          cover_letter=body.cover_letter, regenerate=body.regenerate)
    db.commit()
    if build:
        from app.core.celery_tasks import build_apply_pack_task
        build_apply_pack_task.apply_async(args=[pack.id, user.id], queue=settings.default_queue)
    if pack.status == "pending":
        response.status_code = 202
    return _pack_out(pack)


@router.get("/prep/{pack_id}", response_model=PrepPackOut)
def get_prep(pack_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    pack = db.query(ApplyPack).filter(ApplyPack.id == pack_id, ApplyPack.user_id == user.id).first()
    if pack is None:
        raise HTTPException(404, detail={"code": "pack_not_found", "message": "No prep pack with this id."})
    return _pack_out(pack)


# ---------------------------------------------------------------- P4b: apply engine
#
#   GET/PUT /apply/settings                level, risk notice, apply hours, pause; today's usage and caps
#   POST    /apply/runs                    start a run: mode auto (Greenhouse/Lever/AI form reader, level 2)
#                                          or fast (LinkedIn)
#   GET     /apply/runs?state=waiting      the review queue (waiting = awaiting_input/_review + needs_user)
#   GET     /apply/runs/{id}
#   POST    /apply/runs/{id}/answer        answer required questions ({answers: {key: value}})
#   POST    /apply/runs/{id}/approve       level 2: submit this run (after a 30-90 s pacing gap)
#   POST    /apply/runs/{id}/cancel
#   POST    /apply/runs/{id}/done          you finished it on the real page ({submitted: true|false})
#   POST    /apply/runs/{id}/browser       continue a stopped run in your own browser (extension, P8b E3b)
#   GET     /apply/runs/{id}/proofs[/{proof_id}]   screenshots (PNG), owner only

from app.models.apply import ApplyRun  # noqa: E402
from app.models.apply_schemas import (  # noqa: E402
    ApplySettingsIn, ApplySettingsOut, LiveStart, ProofOut, RunAnswers, RunDone, RunField, RunIn, RunList, RunOut,
)
from app.services import apply_engine, entitlements, extension, live_takeover  # noqa: E402

WAITING = ("awaiting_login", "awaiting_input", "awaiting_review", "needs_user")


def _run_out(db: Session, run: ApplyRun) -> RunOut:
    job = db.get(Job, run.job_id)
    plan = run.plan or {}
    fields = [RunField(**{**{k: v for k, v in p.items() if k != "sealed"}, "value": apply_engine.plan_value(p)})
              for p in plan.get("fields", [])]
    return RunOut(
        id=run.id, job_id=run.job_id, title=job.title if job else "", company=job.company if job else "",
        mode=run.mode, runner=plan.get("runner") or "worker", level=run.level, adapter=run.adapter, apply_url=run.apply_url, state=run.state,
        message=run.message, fields=fields, files=plan.get("files") or {}, resume_id=run.resume_id,
        pack_id=run.pack_id, application_id=run.application_id, proofs=len(apply_engine.proofs(db, run)),
        steps=plan.get("steps") or [], vision=plan.get("vision"), takeover=live_takeover.takeover_out(db, run),
        approved_at=run.approved_at, submitted_at=run.submitted_at, created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _settings_out(db: Session, user) -> ApplySettingsOut:
    s = apply_engine.get_settings_row(db, user.id)
    cfg = get_settings()
    return ApplySettingsOut(
        level=s.level, max_level=entitlements.effective(db, user.id, "auto_apply").max_level or 1,
        risk_accepted_at=s.risk_accepted_at, hours_start=s.hours_start, hours_end=s.hours_end, timezone=s.timezone,
        paused=s.paused, usage_today=apply_engine.usage_today(db, user.id, s),
        allow_generic=s.allow_generic, generic_submit_enabled=cfg.GENERIC_APPLY_SUBMIT,
        extension_assist=s.extension_assist, extension_connected=extension.has_connection(db, user.id),
        fast_notice_accepted_at=s.fast_notice_at,
        caps={"auto_per_day": cfg.APPLY_DAILY_CAP, "per_platform_per_day": cfg.APPLY_PLATFORM_DAILY_CAP,
              "fast_per_day": cfg.FAST_APPLY_DAILY_CAP, "company_cooldown_days": cfg.APPLY_COMPANY_COOLDOWN_DAYS,
              "generic_per_day": cfg.APPLY_GENERIC_DAILY_CAP},
    )


@router.get("/settings", response_model=ApplySettingsOut)
def get_apply_settings(user=Depends(get_current_user), db: Session = Depends(get_db)):
    out = _settings_out(db, user)
    db.commit()
    return out


@router.put("/settings", response_model=ApplySettingsOut)
def put_apply_settings(req: ApplySettingsIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    apply_engine.update_settings(db, user, level=req.level, accept_risk=req.accept_risk_notice,
                                 hours_start=req.hours_start, hours_end=req.hours_end, tz=req.timezone,
                                 paused=req.paused, allow_generic=req.allow_generic,
                                 extension_assist=req.extension_assist, accept_fast_notice=req.accept_fast_notice)
    db.commit()
    return _settings_out(db, user)


@router.post("/runs", response_model=RunOut, status_code=202)
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLY_RUNS)
def start_run(request: Request, body: RunIn, user=Depends(require_feature("auto_apply")),
              db: Session = Depends(get_db)):
    """Preflight (switches, level, risk notice, caps, duplicates, company
    cooldown, resume) and a queued run; a worker on the apply queue opens
    the form next. Nothing is submitted until you approve the run.
    Fast apply with the browser extension connected starts `in_browser`
    (runner `extension`): open the job page and the extension fills it."""
    run = apply_engine.create_run(db, user, _job_or_404(db, body.job_id), mode=body.mode)
    if run.state == "queued":
        from app.core.celery_tasks import apply_prepare_task
        apply_prepare_task.apply_async(args=[run.id], queue=settings.apply_queue)
    return _run_out(db, run)


@router.get("/runs", response_model=RunList)
def list_runs(state: str | None = None, job_id: str | None = None, limit: int = 50, offset: int = 0,
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    """state: a run state, or `waiting` (needs you). job_id: the runs for one job (newest first)."""
    live_takeover.expire_waiting(db, user.id)
    q = db.query(ApplyRun).filter(ApplyRun.user_id == user.id)
    if job_id:
        q = q.filter(ApplyRun.job_id == job_id)
    if state == "waiting":
        q = q.filter(ApplyRun.state.in_(WAITING))
    elif state:
        q = q.filter(ApplyRun.state == state)
    total = q.count()
    rows = q.order_by(ApplyRun.updated_at.desc()).offset(max(0, offset)).limit(min(max(1, limit), 200)).all()
    return {"runs": [_run_out(db, r) for r in rows], "total": total}


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    live_takeover.expire_waiting(db, user.id)
    return _run_out(db, apply_engine.get_owned(db, user.id, run_id))


@router.post("/runs/{run_id}/answer", response_model=RunOut)
def answer_run(run_id: str, body: RunAnswers, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = apply_engine.answer(db, apply_engine.get_owned(db, user.id, run_id), body.answers)
    if run.state == "queued":  # a multi-page form (AI form reader): read the pages after this answer
        from app.core.celery_tasks import apply_prepare_task
        apply_prepare_task.apply_async(args=[run.id], queue=settings.apply_queue)
    return _run_out(db, run)


@router.post("/runs/{run_id}/approve", response_model=RunOut)
def approve_run(run_id: str, user=Depends(require_feature("auto_apply")), db: Session = Depends(get_db)):
    run, countdown = apply_engine.approve(db, user, apply_engine.get_owned(db, user.id, run_id))
    from app.core.celery_tasks import apply_submit_task
    apply_submit_task.apply_async(args=[run.id], queue=settings.apply_queue, countdown=countdown)
    return _run_out(db, run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = apply_engine.get_owned(db, user.id, run_id)
    if run.state == "awaiting_login":
        live_takeover.close_session(run.id)
    return _run_out(db, apply_engine.cancel(db, run))


# ---------------------------------------------------------------- QA11 A: sign in on a live view
#
#   POST /apply/runs/{id}/live            open (or reopen) the live browser on the stop page -> stream token
#   WS   /apply/runs/{id}/live            first message {"type": "auth", "token"}; then JPEG frames + JSON events
#                                         out, input in ({type: click|move|wheel|key|text|back|reload, ...})
#   POST /apply/runs/{id}/live/continue   you are signed in: the run carries on with that browser's cookies
#   POST /apply/runs/{id}/live/give-up    finish it on the job page instead (needs_user)

@router.post("/runs/{run_id}/live", response_model=LiveStart)
@limiter.limit("30/hour")
def start_live(request: Request, run_id: str, user=Depends(require_feature("auto_apply")),
               db: Session = Depends(get_db)):
    """Opens a live view of a browser on the server at the page that stopped this run (`awaiting_login`).
    Sign in, create the account or do the check there yourself (your typing goes straight to the page and is
    never stored), then call continue."""
    return live_takeover.start(db, apply_engine.get_owned(db, user.id, run_id))


@router.post("/runs/{run_id}/live/continue", response_model=RunOut)
def continue_live(run_id: str, user=Depends(require_feature("auto_apply")), db: Session = Depends(get_db)):
    run = live_takeover.proceed(db, apply_engine.get_owned(db, user.id, run_id))
    if run.state == "submitting":
        from app.core.celery_tasks import apply_submit_task
        apply_submit_task.apply_async(args=[run.id], queue=settings.apply_queue)
    elif run.state == "queued":
        from app.core.celery_tasks import apply_prepare_task
        apply_prepare_task.apply_async(args=[run.id], queue=settings.apply_queue)
    return _run_out(db, run)


@router.post("/runs/{run_id}/live/give-up", response_model=RunOut)
def give_up_live(run_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return _run_out(db, live_takeover.give_up(db, apply_engine.get_owned(db, user.id, run_id)))


@router.websocket("/runs/{run_id}/live")
async def live_stream(ws: WebSocket, run_id: str):
    """Relays the live browser: frames down, your clicks and keys up. Messages over 2 kB are dropped."""
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, RuntimeError):
        first = {}
    claims = live_takeover.consume_token(str(first.get("token") or ""), run_id) if isinstance(first, dict) else None
    if claims is None:
        await _ws_close(ws, 4401, "auth_failed", "This stream token is invalid, expired or already used.")
        return
    from app.core.db import SessionLocal
    db = SessionLocal()
    try:
        run = db.query(ApplyRun).filter(ApplyRun.id == run_id, ApplyRun.user_id == claims["sub"]).first()
        waiting = run is not None and run.state == "awaiting_login"
    finally:
        db.close()
    if not waiting:
        await _ws_close(ws, 4409, "run_not_waiting_login", "This run is not waiting for you to sign in.")
        return
    ended = False
    try:
        async with live_takeover.connect_service(run_id) as up:
            async def down() -> None:
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            async def upward() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    text = msg.get("text")
                    if text and len(text) <= 2048:
                        await up.send(text)

            tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(upward())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            ended = tasks[0] in done
    except Exception as e:  # noqa: BLE001 — the service is down or the session is gone
        logger.info("live stream for run=%s ended: %s", run_id, type(e).__name__)
        ended = True
    if ended:
        await _ws_close(ws, 4410, "live_ended", "The live view closed. Open it again to carry on.")


async def _ws_close(ws: WebSocket, code: int, err: str, message: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "code": err, "message": message}))
        await ws.close(code=code)
    except Exception:  # noqa: BLE001
        pass


@router.post("/runs/{run_id}/done", response_model=RunOut)
def finish_run(run_id: str, body: RunDone, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = apply_engine.mark_done(db, apply_engine.get_owned(db, user.id, run_id), submitted=body.submitted)
    return _run_out(db, run)


@router.post("/runs/{run_id}/browser", response_model=RunOut)
def open_run_in_browser(run_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """A run waiting for you continues in your own browser: open its apply page and the Job Finder extension's
    "Fill this form" fills it with this run's answers. Needs a connected extension."""
    run = apply_engine.open_in_browser(db, user, apply_engine.get_owned(db, user.id, run_id))
    return _run_out(db, run)


@router.get("/runs/{run_id}/proofs", response_model=list[ProofOut])
def list_proofs(run_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = apply_engine.get_owned(db, user.id, run_id)
    return [ProofOut(id=p.id, kind=p.kind, confirmation_text=p.confirmation_text, page_url=p.page_url,
                     created_at=p.created_at) for p in apply_engine.proofs(db, run)]


@router.get("/runs/{run_id}/proofs/{proof_id}", response_class=Response,
            responses={200: {"content": {"image/png": {}}}})
def get_proof(run_id: str, proof_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = apply_engine.get_owned(db, user.id, run_id)
    proof = next((p for p in apply_engine.proofs(db, run) if p.id == proof_id), None)
    if proof is None:
        raise HTTPException(404, detail={"code": "proof_not_found", "message": "No screenshot with this id."})
    return Response(apply_engine.load_proof(proof), media_type="image/png",
                    headers={"Cache-Control": "private, no-store"})
