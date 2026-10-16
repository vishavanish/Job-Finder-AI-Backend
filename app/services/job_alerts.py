"""
app/services/job_alerts.py
-----------------------------
Saved job alerts (P3.2). A user saves up to JOB_ALERTS_MAX_PER_USER alerts
(search profile + local hour + minimum score). Celery beat ticks every
JOB_ALERT_TICK_SECONDS; each due alert is claimed with a conditional UPDATE
on next_run_at (two beats can't run it twice) and run as one task that
goes through the normal pipeline path (_process_job): filters, seen-skip,
scoring, the feed. Jobs at or above min_score that the user was never
shown are stored on the run and emailed as a digest. Because shown jobs
become "seen", a job is never sent twice.

Timestamps in these tables are naive UTC (as SQLite returns them).
"""
from __future__ import annotations

import logging
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.alerts import JobAlert, JobAlertRun
from app.models.jobs import Job, UserJob
from app.models.profile import SearchProfile
from app.models.user import User
from app.services import entitlements

logger = logging.getLogger("job_finder_api.alerts")
LAST_TICK_KEY = "alerts:last_tick"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def valid_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone '{name}' (use an IANA name such as Asia/Kolkata)")
    return name


def _minute(alert_id: str) -> int:
    """A fixed minute per alert spreads runs over the hour (gentler on LinkedIn)."""
    return zlib.crc32(alert_id.encode()) % 60


def next_run_at(alert: JobAlert, *, after: datetime | None = None) -> datetime:
    """Next time (naive UTC) the alert's local hour comes round after `after`."""
    tz = ZoneInfo(alert.timezone)
    after_utc = (after or utcnow()).replace(tzinfo=timezone.utc)
    local = after_utc.astimezone(tz)
    candidate = local.replace(hour=alert.hour_local, minute=_minute(alert.id), second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- CRUD

def list_alerts(db: Session, user_id: str) -> list[JobAlert]:
    return db.query(JobAlert).filter(JobAlert.user_id == user_id).order_by(JobAlert.created_at).all()


def get_owned(db: Session, user_id: str, alert_id: str) -> JobAlert:
    row = db.get(JobAlert, alert_id)
    if row is None or row.user_id != user_id:
        raise _error(404, "alert_not_found", "No alert with this id.")
    return row


def _check_profile(db: Session, user_id: str, profile_id: str) -> None:
    profile = db.get(SearchProfile, profile_id)
    if profile is None or profile.user_id != user_id:
        raise _error(422, "search_profile_not_found", "Pick one of your saved search profiles.")


def _apply(row: JobAlert, data: dict) -> None:
    for field in ("name", "search_profile_id", "hour_local", "timezone", "min_score", "email_digest", "enabled"):
        setattr(row, field, data[field])
    row.next_run_at = next_run_at(row) if row.enabled else None


def create_alert(db: Session, user_id: str, data: dict) -> JobAlert:
    if len(list_alerts(db, user_id)) >= get_settings().JOB_ALERTS_MAX_PER_USER:
        raise _error(409, "alert_limit",
                     f"You can keep up to {get_settings().JOB_ALERTS_MAX_PER_USER} alerts. Delete one first.")
    _check_profile(db, user_id, data["search_profile_id"])
    row = JobAlert(id=str(uuid.uuid4()), user_id=user_id)  # id first: it fixes the run minute
    _apply(row, data)
    db.add(row)
    db.commit()
    return row


def update_alert(db: Session, row: JobAlert, data: dict) -> JobAlert:
    _check_profile(db, row.user_id, data["search_profile_id"])
    _apply(row, data)
    db.commit()
    return row


def delete_alert(db: Session, row: JobAlert) -> None:
    db.delete(row)
    db.commit()


def runs(db: Session, alert: JobAlert, limit: int = 30) -> list[JobAlertRun]:
    return (db.query(JobAlertRun).filter(JobAlertRun.alert_id == alert.id)
            .order_by(JobAlertRun.started_at.desc()).limit(limit).all())


def get_owned_run(db: Session, user_id: str, run_id: str) -> JobAlertRun:
    row = db.get(JobAlertRun, run_id)
    if row is None or row.user_id != user_id:
        raise _error(404, "alert_run_not_found", "No alert run with this id.")
    return row


def run_jobs(db: Session, run: JobAlertRun) -> list[tuple[Job, UserJob]]:
    ids = list(run.job_ids or [])
    if not ids:
        return []
    rows = (db.query(Job, UserJob).join(UserJob, UserJob.job_id == Job.id)
            .filter(UserJob.user_id == run.user_id, Job.id.in_(ids)).all())
    order = {job_id: i for i, job_id in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r[0].id, len(order)))


# ---------------------------------------------------------------- scheduling

def claim_due(db: Session, *, now: datetime | None = None, limit: int = 100) -> list[str]:
    """Ids of due alerts this caller won (next_run_at moved forward atomically)."""
    now = now or utcnow()
    due = (db.query(JobAlert).filter(JobAlert.enabled.is_(True), JobAlert.next_run_at.isnot(None),
                                     JobAlert.next_run_at <= now)
           .order_by(JobAlert.next_run_at).limit(limit).all())
    won = []
    for alert in due:
        new_next = next_run_at(alert, after=now)
        claimed = db.execute(update(JobAlert)
                             .where(JobAlert.id == alert.id, JobAlert.next_run_at == alert.next_run_at)
                             .values(next_run_at=new_next)).rowcount
        db.commit()
        if claimed:
            won.append(alert.id)
    get_redis().set(LAST_TICK_KEY, datetime.now(timezone.utc).isoformat())
    return won


def last_tick() -> str | None:
    try:
        return get_redis().get(LAST_TICK_KEY)
    except Exception:  # noqa: BLE001 — health must not fail on this
        return None


def may_run(db: Session, user: User | None) -> str | None:
    """Why an alert can't run for this user now (None = it can). Counts one
    `job_alerts` use against the daily quota when it can."""
    if user is None or not user.is_active:
        return "account disabled"
    for feature in ("job_search", "scoring"):
        if not entitlements.effective(db, user.id, feature).enabled:
            return f"{entitlements.FEATURE_LABELS[feature]} is not enabled for this account"
    try:
        entitlements.check_feature(db, user, "job_alerts")
    except HTTPException as e:
        return str(e.detail)
    return None


# ---------------------------------------------------------------- digest

def digest_text(alert: JobAlert, jobs: list[dict]) -> tuple[str, str]:
    settings = get_settings()
    subject = f"{len(jobs)} new job match{'es' if len(jobs) != 1 else ''} — {alert.name}"
    lines = [f"New jobs for your alert \"{alert.name}\" (score {alert.min_score}+):", ""]
    for i, job in enumerate(jobs, 1):
        score = job.get("llm_score")
        extras = [f"score {score:.0f}" if isinstance(score, (int, float)) else None,
                  "posted < 24 h ago" if job.get("early") else None,
                  f"{job['applicants']} applicants" if job.get("applicants") else None]
        lines += [f"{i}. {job.get('title', '')} — {job.get('company', '')} ({job.get('location', '')})",
                  f"   {' · '.join(e for e in extras if e)}",
                  f"   {job.get('llm_reason', '')}".rstrip(),
                  f"   {job.get('url', '')}", ""]
    where = f" at {settings.APP_PUBLIC_URL}" if settings.APP_PUBLIC_URL else ""
    lines += [f"Turn this alert off or change it in Job Finder AI → Alerts{where}."]
    return subject, "\n".join(line for line in lines if line is not None)
