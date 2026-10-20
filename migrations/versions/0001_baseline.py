"""Baseline: the schema that Base.metadata.create_all() produced before
Alembic was introduced.

Idempotent on purpose: databases created by the old create_all() at
startup already have these tables, so each table is created only if it
is missing. That lets `alembic upgrade head` run unchanged on both the
existing Supabase database and a brand-new one — no manual `stamp`.

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _missing(table: str) -> bool:
    return table not in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _missing("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("hashed_password", sa.String(), nullable=False),
            sa.Column("name", sa.String(255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_users_email", "users", ["email"], unique=True)

    if _missing("applications"):
        op.create_table(
            "applications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("job_title", sa.String(500), nullable=True),
            sa.Column("company", sa.String(255), nullable=True),
            sa.Column("source", sa.String(100), nullable=True),
            sa.Column("job_url", sa.Text(), nullable=True),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("status_source", sa.String(32), nullable=False),
            sa.Column("last_email_snippet", sa.Text(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "job_url", name="uq_application_user_job"),
        )
        op.create_index("ix_applications_user_id", "applications", ["user_id"])
        op.create_index("ix_applications_company", "applications", ["company"])
        op.create_index("ix_applications_status", "applications", ["status"])
        op.create_index("ix_application_user_status", "applications", ["user_id", "status"])

    if _missing("application_status_events"):
        op.create_table(
            "application_status_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.id"), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("status_source", sa.String(32), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_application_status_events_application_id", "application_status_events", ["application_id"])
        op.create_index("ix_application_status_events_created_at", "application_status_events", ["created_at"])

    if _missing("pipeline_runs"):
        op.create_table(
            "pipeline_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("total_keywords", sa.Integer(), nullable=False),
            sa.Column("keywords_completed", sa.Integer(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_pipeline_runs_user_id", "pipeline_runs", ["user_id"])
        op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])

    if _missing("pipeline_run_jobs"):
        op.create_table(
            "pipeline_run_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("pipeline_runs.id"), nullable=False),
            sa.Column("source", sa.String(100), nullable=True),
            sa.Column("title", sa.String(500), nullable=True),
            sa.Column("company", sa.String(255), nullable=True),
            sa.Column("location", sa.String(255), nullable=True),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("posted", sa.String(64), nullable=True),
            sa.Column("keyword_score", sa.Float(), nullable=True),
            sa.Column("llm_score", sa.Float(), nullable=True),
            sa.Column("llm_reason", sa.Text(), nullable=True),
            sa.Column("auto_apply_capable", sa.String(5), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_pipeline_run_jobs_run_id", "pipeline_run_jobs", ["run_id"])
        op.create_index("ix_pipeline_run_jobs_created_at", "pipeline_run_jobs", ["created_at"])
        op.create_index("ix_pipeline_run_jobs_run_created", "pipeline_run_jobs", ["run_id", "created_at"])


def downgrade() -> None:
    # Dropping the baseline would delete all data; refuse instead.
    raise RuntimeError("Refusing to downgrade below the baseline.")
