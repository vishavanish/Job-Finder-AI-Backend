"""
app/models/pipeline_run.py
-----------------------------
Two tables backing the streaming pipeline:

  - PipelineRun: one row per POST /pipeline/run call. Tracks aggregate
    status so the frontend can show a progress bar without re-deriving
    it from N sub-task states on every poll.

  - PipelineRunJob: one row per job that has been searched AND scored.
    Written the INSTANT a single job finishes scoring — not batched,
    not held until the whole run completes. This is what makes true
    incremental streaming possible: GET /pipeline/{run_id}/jobs just
    reads whatever rows exist right now.

CONCURRENCY: multiple group() members (one per keyword) write to
PipelineRunJob concurrently, each from its own Celery task. Every write
is a single INSERT of one row — no read-modify-write, so there's no
race condition to guard against; Postgres handles concurrent inserts
into the same table natively. keywords_completed on PipelineRun IS a
read-modify-write (increment), so that one uses an atomic UPDATE
statement (see pipeline_progress.py), never `row.x += 1` after a
SELECT, which would lose increments under concurrent writers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Text, ForeignKey, Integer, Float, Index
from sqlalchemy.orm import relationship

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)

    status = Column(String(32), default="pending", nullable=False, index=True)  # pending|running|completed|failed
    total_keywords = Column(Integer, default=0, nullable=False)
    keywords_completed = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    jobs = relationship("PipelineRunJob", back_populates="run", cascade="all, delete-orphan")


class PipelineRunJob(Base):
    """One row per job that has completed search+score. Append-only —
    a run never updates an existing row here, only inserts new ones as
    jobs finish. This is the table GET /pipeline/{run_id}/jobs reads."""
    __tablename__ = "pipeline_run_jobs"
    __table_args__ = (
        Index("ix_pipeline_run_jobs_run_created", "run_id", "created_at"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey("pipeline_runs.id"), nullable=False, index=True)

    source = Column(String(100), default="")
    title = Column(String(500), default="")
    company = Column(String(255), default="")
    location = Column(String(255), default="")
    url = Column(Text, default="")
    description = Column(Text, default="")
    posted = Column(String(64), default="")

    keyword_score = Column(Float, nullable=True)
    llm_score = Column(Float, nullable=True)
    llm_reason = Column(Text, nullable=True)
    auto_apply_capable = Column(String(5), default="false")  # "true"/"false" — see note below

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    run = relationship("PipelineRun", back_populates="jobs")