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

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
#   GET     /apply/runs/{id}/proofs[/{proof_id}]   screenshots (PNG), owner only

from app.models.apply import ApplyRun  # noqa: E402
from app.models.apply_schemas import (  # noqa: E402
    ApplySettingsIn, ApplySettingsOut, ProofOut, RunAnswers, RunDone, RunField, RunIn, RunList, RunOut,
)
from app.services import apply_engine, entitlements  # noqa: E402

WAITING = ("awaiting_input", "awaiting_review", "needs_user")


def _run_out(db: Session, run: ApplyRun) -> RunOut:
    job = db.get(Job, run.job_id)
    plan = run.plan or {}
    fields = [RunField(**{**{k: v for k, v in p.items() if k != "sealed"}, "value": apply_engine.plan_value(p)})
              for p in plan.get("fields", [])]
    return RunOut(
        id=run.id, job_id=run.job_id, title=job.title if job else "", company=job.company if job else "",
        mode=run.mode, level=run.level, adapter=run.adapter, apply_url=run.apply_url, state=run.state,
        message=run.message, fields=fields, files=plan.get("files") or {}, resume_id=run.resume_id,
        pack_id=run.pack_id, application_id=run.application_id, proofs=len(apply_engine.proofs(db, run)),
        steps=plan.get("steps") or [], vision=plan.get("vision"),
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
                                 paused=req.paused, allow_generic=req.allow_generic)
    db.commit()
    return _settings_out(db, user)


@router.post("/runs", response_model=RunOut, status_code=202)
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLY_RUNS)
def start_run(request: Request, body: RunIn, user=Depends(require_feature("auto_apply")),
              db: Session = Depends(get_db)):
    """Preflight (switches, level, risk notice, caps, duplicates, company
    cooldown, resume) and a queued run; a worker on the apply queue opens
    the form next. Nothing is submitted until you approve the run."""
    run = apply_engine.create_run(db, user, _job_or_404(db, body.job_id), mode=body.mode)
    from app.core.celery_tasks import apply_prepare_task
    apply_prepare_task.apply_async(args=[run.id], queue=settings.apply_queue)
    return _run_out(db, run)


@router.get("/runs", response_model=RunList)
def list_runs(state: str | None = None, job_id: str | None = None, limit: int = 50, offset: int = 0,
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    """state: a run state, or `waiting` (needs you). job_id: the runs for one job (newest first)."""
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
    return _run_out(db, apply_engine.cancel(db, apply_engine.get_owned(db, user.id, run_id)))


@router.post("/runs/{run_id}/done", response_model=RunOut)
def finish_run(run_id: str, body: RunDone, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = apply_engine.mark_done(db, apply_engine.get_owned(db, user.id, run_id), submitted=body.submitted)
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
