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
from app.services.job_dedupe import job_key as compute_job_key

logger = logging.getLogger("job_finder_api.applications_store")


def record_status(
    *,
    user_id: str,
    job: dict,
    status: str,
    status_source: str = "browser_apply",
    note: str | None = None,
) -> Application:
    """Upserts the current-state row for this user's job and appends an
    immutable history event in the same transaction. The row is found by
    job URL first, then by job_key — so the same job reached from another
    source updates the existing row (its URL is kept in alias_urls)
    instead of creating a second application.

    Raises SQLAlchemyError on failure — callers must not swallow this;
    a failed status write should surface, not disappear silently (this
    was the root cause of the original CSV-based tracking bug)."""
    if not job.get("url"):
        raise ValueError("record_status requires job['url'] to identify the application")

    key = compute_job_key(job)
    db = SessionLocal()
    try:
        existing = (
            db.query(Application)
            .filter(Application.user_id == user_id, Application.job_url == job["url"])
            .first()
        ) or (
            db.query(Application)
            .filter(Application.user_id == user_id, Application.job_key == key)
            .first()
        )

        if existing:
            existing.status = status
            existing.status_source = status_source
            existing.job_key = existing.job_key or key
            if job["url"] != existing.job_url and job["url"] not in (existing.alias_urls or []):
                # reassign, not append — JSON columns don't track in-place mutation
                existing.alias_urls = [*(existing.alias_urls or []), job["url"]]
            row = existing
        else:
            row = Application(
                user_id=user_id,
                job_title=job.get("title", ""),
                company=job.get("company", ""),
                source=job.get("source", ""),
                job_url=job["url"],
                job_key=key,
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