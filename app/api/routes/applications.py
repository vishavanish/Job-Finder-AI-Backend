"""
app/api/routes/applications.py
---------------------------------
Reads from the `applications` table (via app.models.application) instead
of a CSV. Every route is scoped to the authenticated user — a caller only
ever sees/modifies their own applications.

CAVEAT (unchanged from the original CSV design): status reflects what
the /apply flow observed when it opened/prefilled a job, a manual PATCH,
or what an email parser later detects — NOT that a human confirmed they
clicked submit inside the browser, unless status_source is 'manual' or
'email_parser'.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.models.application import Application
from app.models.jobs import Job
from app.models.email_schemas import ApplicationTimeline, TimelineEvent, TrackerStats
from app.models.schemas import ApplicationLogEntry, ApplicationLogResult, ApplicationStatusUpdate
from app.services import job_store
from app.services.applications_store import record_status

router = APIRouter(prefix="/applications", tags=["applications"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger("job_finder_api.applications")


def _to_entry(row: Application) -> ApplicationLogEntry:
    return ApplicationLogEntry(
        id=row.id,
        date=row.applied_at.date().isoformat() if row.applied_at else "",
        source=row.source or "",
        title=row.job_title or "",
        company=row.company or "",
        url=row.job_url or "",
        status=row.status or "",
        status_source=row.status_source or "",
        updated_at=row.updated_at,
    )


@router.get("", response_model=ApplicationLogResult)
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLICATIONS)
async def list_applications(
    request: Request,
    status: Optional[str] = Query(None, description="Exact match, e.g. 'opened' or 'rejected'."),
    company: Optional[str] = Query(None, description="Case-insensitive substring match against company."),
    source: Optional[str] = Query(None, description="Exact match against source, e.g. 'LinkedIn'."),
    since: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD) — only rows applied on or after this date."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Application).filter(Application.user_id == user.id)

    if status:
        query = query.filter(Application.status == status)
    if company:
        query = query.filter(Application.company.ilike(f"%{company}%"))
    if source:
        query = query.filter(Application.source == source)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(400, "since must be an ISO date, e.g. 2026-08-01")
        query = query.filter(Application.applied_at >= since_dt)

    total = query.count()
    rows = query.order_by(Application.applied_at.desc()).offset(offset).limit(limit).all()

    return ApplicationLogResult(
        applications=[_to_entry(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# Statuses that mean the application went out (anything past "opened"/prefill).
_SENT = {"submitted", "emailed", "done", "acknowledged", "assessment", "interview", "offer", "accepted", "declined",
         "rejected", "withdrawn", "ghosted"}
_FUNNEL = ["acknowledged", "assessment", "interview", "offer"]


@router.get("/stats", response_model=TrackerStats)
def tracker_stats(days: int = Query(0, ge=0, le=3650, description="0 = all time"),
                  user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Dashboard numbers (CLAUDE.md 7.8): funnel (reached at least each stage, so an interview also counts as
    acknowledged), this week, by status and source, and emails waiting for review."""
    from app.models.email import EmailEvent
    from app.services.email_sync import STATUS_RANK

    q = db.query(Application).filter(Application.user_id == user.id)
    if days:
        q = q.filter(Application.applied_at >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days))
    rows = q.all()
    by_status = Counter(r.status for r in rows)
    sent = [r for r in rows if r.status in _SENT]
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    def reached(stage: str) -> int:
        rank = STATUS_RANK[stage]
        n = 0
        for r in sent:
            if r.status in ("accepted", "declined"):
                n += 1  # had an offer
            elif r.status == "rejected":
                # how far it got before the rejection: the best status in its history
                best = max((STATUS_RANK.get(e.status, 0) for e in r.events if e.status != "rejected"), default=1)
                n += best >= rank
            else:
                n += STATUS_RANK.get(r.status, 1) >= rank
        return n

    applied_at = lambda r: r.applied_at if r.applied_at.tzinfo else r.applied_at.replace(tzinfo=timezone.utc)  # noqa: E731
    return TrackerStats(
        total=len(sent), this_week=sum(1 for r in sent if applied_at(r) >= week_ago),
        funnel={"applied": len(sent), **{s: reached(s) for s in _FUNNEL}},
        rejected=by_status.get("rejected", 0), by_status=dict(by_status),
        by_source=dict(Counter(r.source or "Unknown" for r in sent)),
        needs_review=db.query(EmailEvent).filter(EmailEvent.user_id == user.id,
                                                 EmailEvent.state == "needs_review").count(),
    )


@router.get("/{application_id}/events", response_model=ApplicationTimeline)
def application_events(application_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """The timeline: every status change with where it came from (apply_engine, manual, email_parser) and, for
    email changes, which email caused it."""
    from app.models.email import EmailEvent

    row = db.query(Application).filter(Application.id == application_id, Application.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "no application found with this id")
    email_ids = [e.email_event_id for e in row.events if e.email_event_id]
    emails = {m.id: m for m in db.query(EmailEvent).filter(EmailEvent.id.in_(email_ids),
                                                           EmailEvent.user_id == user.id).all()} if email_ids else {}
    events = []
    for e in row.events:
        m = emails.get(e.email_event_id or "")
        events.append(TimelineEvent(
            status=e.status, status_source=e.status_source, note=e.note, created_at=e.created_at,
            email={"sender": m.sender, "subject": m.subject, "received_at": m.received_at.isoformat()} if m else None,
        ))
    return ApplicationTimeline(application_id=row.id, events=events)


@router.get("/{application_id}", response_model=ApplicationLogEntry)
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLICATIONS)
async def get_application(
    request: Request,
    application_id: str,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(Application)
        .filter(Application.id == application_id, Application.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "no application found with this id")
    return _to_entry(row)


@router.patch("/{application_id}", response_model=ApplicationLogEntry)
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLICATIONS)
async def update_application_status(
    request: Request,
    application_id: str,
    body: ApplicationStatusUpdate,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manual status override — e.g. mark 'submitted' once you've actually
    clicked Submit inside the browser."""
    row = (
        db.query(Application)
        .filter(Application.id == application_id, Application.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "no application found with this id")

    updated = record_status(
        user_id=user.id,
        job={"url": row.job_url, "title": row.job_title, "company": row.company, "source": row.source},
        status=body.status,
        status_source="manual",
    )
    logger.info("application %s manually set to status=%s by user=%s", application_id, body.status, user.id)
    return _to_entry(updated)

class ManualApplyIn(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=36, description="Canonical job id (a feed entry's `id`)")
    via: Literal["manual", "email"] = Field(
        "manual", description="manual: applied on the job's page (status `submitted`); "
                              "email: sent the Email apply draft (status `emailed`)")


@router.post("/manual", response_model=ApplicationLogEntry, status_code=201)
@limiter.limit(lambda: get_settings().RATE_LIMIT_APPLICATIONS)
async def mark_applied_manually(
    request: Request,
    body: ManualApplyIn,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """"Apply manually → Mark as applied" (CLAUDE.md 7.7): the user applied
    on the job's own page (`submitted`) or sent the Email apply draft
    (`emailed`, P4a). Recorded with status_source `manual`; the job moves
    to the user's Applied list so later searches skip it."""
    job = db.get(Job, body.job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "job_not_found", "message": "No job with this id."})
    row = record_status(
        user_id=user.id,
        job={"url": job.url, "title": job.title, "company": job.company, "source": job.best_source,
             "location": job.location},
        status="emailed" if body.via == "email" else "submitted",
        status_source="manual",
        note="sent the application email" if body.via == "email" else "marked as applied by the user",
    )
    job_store.set_state(db, user_id=user.id, job_id=job.id, state="applied")
    db.commit()
    return _to_entry(row)
