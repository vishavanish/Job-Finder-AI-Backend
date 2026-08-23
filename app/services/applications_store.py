"""
app/services/applications_store.py
--------------------------------------
Single choke point for writing application status. Every caller — the
browser-apply flow, a manual PATCH, or the future email-rejection
parser — goes through record_status(). This guarantees the upsert +
history-event write always happen together, atomically, regardless of
caller, and prevents the current-state table and history table from
ever drifting apart.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from app.core.db import SessionLocal
from app.models.application import Application, ApplicationStatusEvent

logger = logging.getLogger("job_finder_api.applications_store")


def record_status(
    *,
    user_id: str,
    job: dict,
    status: str,
    status_source: str = "browser_apply",
    note: str | None = None,
) -> Application:
    """Upserts the current-state row for (user_id, job['url']) and appends
    an immutable history event in the same transaction.

    Raises SQLAlchemyError on failure — callers must not swallow this;
    a failed status write should surface, not disappear silently (this
    was the root cause of the original CSV-based tracking bug)."""
    if not job.get("url"):
        raise ValueError("record_status requires job['url'] to identify the application")

    db = SessionLocal()
    try:
        existing = (
            db.query(Application)
            .filter(Application.user_id == user_id, Application.job_url == job["url"])
            .first()
        )

        if existing:
            existing.status = status
            existing.status_source = status_source
            row = existing
        else:
            row = Application(
                user_id=user_id,
                job_title=job.get("title", ""),
                company=job.get("company", ""),
                source=job.get("source", ""),
                job_url=job["url"],
                status=status,
                status_source=status_source,
            )
            db.add(row)

        db.flush()  # ensures row.id exists before the event references it

        db.add(ApplicationStatusEvent(
            application_id=row.id,
            status=status,
            status_source=status_source,
            note=note,
        ))
        db.commit()
        db.refresh(row)
        return row
    except SQLAlchemyError:
        db.rollback()
        logger.exception("record_status failed for user_id=%s job_url=%s status=%s", user_id, job.get("url"), status)
        raise
    finally:
        db.close()