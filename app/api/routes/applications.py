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
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.models.application import Application
from app.models.schemas import ApplicationLogEntry, ApplicationLogResult, ApplicationStatusUpdate
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