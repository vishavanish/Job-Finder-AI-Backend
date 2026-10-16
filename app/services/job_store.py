"""
app/services/job_store.py
----------------------------
Canonical jobs and per-user job state (CLAUDE.md 7.5).

`upsert_copy` is called for every scraped copy of a job, by keyword tasks
running in parallel on different workers. It is race-safe without locks:
  - the insert relies on the unique `job_key` (a loser re-reads the row);
  - "is this copy better?" is a single conditional UPDATE, so two copies
    arriving at once cannot overwrite a better one. Better = a better-ranked
    source (LinkedIn > company ATS > board > aggregator, stored in
    `best_source_rank`), otherwise the longer description (job_dedupe.preferred).
It commits per job: these run in workers, one short transaction per copy.

`decide` says what the pipeline does with a job the user may have met
before: skip it, reuse the stored score, or score it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.jobs import Job, JobSourceSeen, UserJob
from app.services.source_info import source_rank

# States set by the user (or the apply engine) that a feed never overwrites.
STICKY_STATES = {"saved", "hidden", "applied"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(value, n: int) -> str:
    return str(value or "")[:n]


def _fields(job: dict) -> dict:
    return {
        "title": _clip(job.get("title"), 500), "company": _clip(job.get("company"), 255),
        "location": _clip(job.get("location"), 255), "url": str(job.get("url") or ""),
        "description": str(job.get("description") or ""), "posted": _clip(job.get("posted"), 64),
        "best_source": _clip(job.get("source"), 100), "best_source_rank": source_rank(job.get("source")),
    }


def upsert_copy(db: Session, job: dict, key: str) -> Job:
    """Stores this copy of a job and returns the canonical row, which
    holds the best copy seen so far (possibly this one)."""
    fields = _fields(job)
    row = db.query(Job).filter(Job.job_key == key).first()
    if row is None:
        try:
            row = Job(job_key=key, **fields)
            db.add(row)
            db.commit()
        except IntegrityError:  # another worker inserted it first
            db.rollback()
            row = db.query(Job).filter(Job.job_key == key).one()
        else:
            _touch_source(db, row.id, fields["best_source"], fields["url"])
            return row

    rank = fields["best_source_rank"]
    longer = func.length(Job.description) < len(fields["description"])
    cond = or_(Job.best_source_rank > rank, (Job.best_source_rank == rank) & longer)
    if not db.execute(update(Job).where(Job.id == row.id, cond).values(**fields, last_seen_at=_now())).rowcount:
        db.execute(update(Job).where(Job.id == row.id).values(last_seen_at=_now()))
    db.commit()
    db.refresh(row)
    _touch_source(db, row.id, fields["best_source"], fields["url"])
    return row


def _touch_source(db: Session, job_id: str, source: str, url: str) -> None:
    source = source or "unknown"
    n = db.execute(update(JobSourceSeen).where(JobSourceSeen.job_id == job_id, JobSourceSeen.source == source)
                   .values(url=url, last_seen_at=_now())).rowcount
    if not n:
        try:
            db.add(JobSourceSeen(job_id=job_id, source=source, url=url))
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            return
    db.commit()


def reload(db: Session, row: Job) -> Job:
    """The row as committed now (another worker may have improved it)."""
    db.expire(row)
    db.refresh(row)
    return row


def as_job(row: Job) -> dict:
    """The canonical copy in the plain-dict shape scrapers produce."""
    return {"id": row.id, "source": row.best_source, "title": row.title, "company": row.company,
            "location": row.location, "url": row.url, "description": row.description, "posted": row.posted}


def sources(db: Session, job_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not job_ids:
        return out
    for job_id, source in (db.query(JobSourceSeen.job_id, JobSourceSeen.source)
                           .filter(JobSourceSeen.job_id.in_(job_ids)).order_by(JobSourceSeen.first_seen_at)):
        out.setdefault(job_id, []).append(source)
    return out


# ---------------------------------------------------------------- user state

def get_user_job(db: Session, user_id: str, job_id: str) -> UserJob | None:
    return db.query(UserJob).filter(UserJob.user_id == user_id, UserJob.job_id == job_id).first()


def decide(uj: UserJob | None, *, include_seen: bool, profile_hash: str) -> str:
    """"score" | "reuse" (stored score is for this profile) | "skip"."""
    if uj is None:
        return "score"
    same_profile = uj.profile_hash == profile_hash and uj.llm_score is not None
    if uj.state in ("hidden", "applied"):
        return "skip"
    if uj.state == "filtered":
        return "skip" if uj.profile_hash == profile_hash else "score"
    # seen / saved
    if not include_seen:
        return "skip"
    return "reuse" if same_profile else "score"


def record_user_job(db: Session, *, user_id: str, job_id: str, state: str, keyword_score: float | None,
                    llm_score: float | None, reason: str | None, profile_hash: str | None,
                    search_profile_id: str | None, run_id: str | None) -> UserJob:
    """Upsert. Saved/hidden/applied are kept; only the scores move."""
    values = {"keyword_score": keyword_score, "llm_score": llm_score, "reason": reason,
              "profile_hash": profile_hash, "search_profile_id": search_profile_id, "last_run_id": run_id}
    uj = get_user_job(db, user_id, job_id)
    if uj is None:
        try:
            uj = UserJob(user_id=user_id, job_id=job_id, state=state, **values)
            db.add(uj)
            db.commit()
            return uj
        except IntegrityError:
            db.rollback()
            uj = get_user_job(db, user_id, job_id)
    for k, v in values.items():
        setattr(uj, k, v)
    if uj.state not in STICKY_STATES:
        uj.state = state
    db.commit()
    return uj


def set_state(db: Session, *, user_id: str, job_id: str, state: str) -> UserJob:
    uj = get_user_job(db, user_id, job_id)
    if uj is None:
        uj = UserJob(user_id=user_id, job_id=job_id, state=state)
        db.add(uj)
    else:
        uj.state = state
    db.flush()
    return uj
