"""
app/api/routes/jobs.py
------------------------
The user's own jobs (CLAUDE.md 7.5). `job_id` is the canonical job id —
the `id` of a pipeline feed entry.

  - GET  /jobs?state=saved          saved (default), hidden or seen jobs
  - POST /jobs/{job_id}/save        keep it in Saved
  - POST /jobs/{job_id}/hide        never show it again
  - POST /jobs/{job_id}/seen        undo save/hide (back to plain "seen")

Seen, hidden and applied jobs are skipped by later pipeline runs; saved
and seen ones come back with `include_seen: true`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models.jobs import Job, UserJob
from app.models.profile_schemas import UserJobList, UserJobOut
from app.services import apply_options, job_store

router = APIRouter(prefix="/jobs", tags=["jobs"])

ACTIONS = {"save": "saved", "hide": "hidden", "seen": "seen"}


def _out(job: Job, uj: UserJob, sources: list[str]) -> UserJobOut:
    fresh = job_store.freshness(job)
    return UserJobOut(id=job.id, title=job.title, company=job.company, location=job.location, url=job.url,
                      source=job.best_source, posted=job.posted, state=uj.state, keyword_score=uj.keyword_score,
                      llm_score=uj.llm_score, reason=uj.reason, sources=sources, match=uj.breakdown,
                      posted_at=fresh["posted_at"], posted_hours_ago=fresh["posted_hours_ago"], early=fresh["early"],
                      applicants=job.applicants, apply_url=job.apply_url,
                      apply_options=apply_options.options(job_store.as_job(job)), updated_at=uj.updated_at)


@router.get("", response_model=UserJobList)
def list_my_jobs(
    state: Literal["saved", "hidden", "seen", "applied"] = "saved",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (db.query(Job, UserJob).join(UserJob, UserJob.job_id == Job.id)
         .filter(UserJob.user_id == user.id, UserJob.state == state))
    total = q.count()
    rows = q.order_by(UserJob.updated_at.desc()).offset(offset).limit(limit).all()
    srcs = job_store.sources(db, [j.id for j, _ in rows])
    return {"jobs": [_out(j, uj, srcs.get(j.id, [])) for j, uj in rows], "total": total}


@router.post("/{job_id}/{action}", response_model=UserJobOut)
def set_job_state(job_id: str, action: Literal["save", "hide", "seen"], user=Depends(get_current_user),
                  db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "job_not_found", "message": "No job with this id."})
    current = job_store.get_user_job(db, user.id, job_id)
    if current is not None and current.state == "applied":
        raise HTTPException(409, detail={"code": "job_applied",
                                         "message": "You applied to this job; its state follows the application."})
    uj = job_store.set_state(db, user_id=user.id, job_id=job_id, state=ACTIONS[action])
    db.commit()
    return _out(job, uj, job_store.sources(db, [job.id]).get(job.id, []))
