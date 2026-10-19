"""
app/api/routes/apply.py
-------------------------
Apply Center (CLAUDE.md 7.7).

  - POST /apply                 legacy: open + prefill LinkedIn Easy Apply on the worker
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


@router.post("", response_model=TaskResponse, status_code=202)
@limiter.limit(settings.RATE_LIMIT_APPLY)
async def start_apply(request: Request, req: ApplyRequest, user=Depends(require_feature("auto_apply"))):
    """Opens top-matched jobs in a real Playwright browser on the Celery
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
        error=pack.error, updated_at=pack.updated_at,
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
