"""
app/api/routes/alerts.py
--------------------------
Saved job alerts (P3.2):

  - GET    /alerts                       my alerts (+ the per-user limit and the addresses a digest can go to)
  - POST   /alerts                       save one (search profile, local hour, time zone, min score, email_to)
  - PUT    /alerts/{id}                  change it (recomputes the next run)
  - DELETE /alerts/{id}
  - POST   /alerts/{id}/run-now          run it now (counts against the daily job_alerts quota)
  - GET    /alerts/{id}/runs             recent runs
  - GET    /alerts/runs/{run_id}/jobs    the new jobs one run found

Runs happen in a worker; Celery beat starts due alerts (CLAUDE.md 10).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.routes.jobs import _out as job_out
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.alert_schemas import AlertIn, AlertList, AlertOut, AlertRunJobs, AlertRunList, AlertRunOut
from app.models.alerts import JobAlert, JobAlertRun
from app.models.schemas import TaskResponse
from app.services import entitlements, job_alerts, job_store

router = APIRouter(prefix="/alerts", tags=["alerts"])
settings = get_settings()


def _require_alerts(db: Session, user) -> None:
    if not entitlements.effective(db, user.id, "job_alerts").enabled:
        raise HTTPException(403, "Your account does not include job alerts. Ask an admin to enable it.")


def _alert_out(row: JobAlert, options: list[str]) -> AlertOut:
    return AlertOut(id=row.id, name=row.name, search_profile_id=row.search_profile_id, hour_local=row.hour_local,
                    timezone=row.timezone, min_score=row.min_score, email_digest=row.email_digest,
                    email_to=row.email_to, sends_to=job_alerts.sends_to(row, options),
                    enabled=row.enabled, last_run_at=row.last_run_at, next_run_at=row.next_run_at,
                    created_at=row.created_at)


def _run_out(row: JobAlertRun) -> AlertRunOut:
    return AlertRunOut(id=row.id, alert_id=row.alert_id, trigger=row.trigger, status=row.status,
                       started_at=row.started_at, finished_at=row.finished_at, counts=row.counts or {},
                       new_jobs=len(row.job_ids or []), emailed_at=row.emailed_at, error=row.error)


@router.get("", response_model=AlertList)
def list_alerts(user=Depends(get_current_user), db: Session = Depends(get_db)):
    options = job_alerts.email_options(db, user)
    return {"alerts": [_alert_out(r, options) for r in job_alerts.list_alerts(db, user.id)],
            "max_alerts": settings.JOB_ALERTS_MAX_PER_USER, "email_options": options}


@router.post("", response_model=AlertOut, status_code=201)
def create_alert(req: AlertIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_alerts(db, user)
    row = job_alerts.create_alert(db, user.id, req.model_dump())
    return _alert_out(row, job_alerts.email_options(db, user))


@router.put("/{alert_id}", response_model=AlertOut)
def update_alert(alert_id: str, req: AlertIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_alerts(db, user)
    row = job_alerts.update_alert(db, job_alerts.get_owned(db, user.id, alert_id), req.model_dump())
    return _alert_out(row, job_alerts.email_options(db, user))


@router.delete("/{alert_id}", status_code=204)
def delete_alert(alert_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    job_alerts.delete_alert(db, job_alerts.get_owned(db, user.id, alert_id))


@router.post("/{alert_id}/run-now", response_model=TaskResponse, status_code=202)
@limiter.limit(settings.RATE_LIMIT_PIPELINE)
def run_now(request: Request, alert_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_alerts(db, user)
    row = job_alerts.get_owned(db, user.id, alert_id)
    from app.core.celery_tasks import run_job_alert
    return dispatch(run_job_alert, args=[row.id, "manual"], queue=settings.default_queue, user_id=user.id)


@router.get("/{alert_id}/runs", response_model=AlertRunList)
def list_runs(alert_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = job_alerts.get_owned(db, user.id, alert_id)
    return {"runs": [_run_out(r) for r in job_alerts.runs(db, row)]}


@router.get("/runs/{run_id}/jobs", response_model=AlertRunJobs)
def run_jobs(run_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    run = job_alerts.get_owned_run(db, user.id, run_id)
    rows = job_alerts.run_jobs(db, run)
    srcs = job_store.sources(db, [j.id for j, _ in rows])
    return {"run": _run_out(run), "jobs": [job_out(j, uj, srcs.get(j.id, [])) for j, uj in rows]}
