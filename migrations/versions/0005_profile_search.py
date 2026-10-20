"""Profile & search (Phase 3): profile + applicant info, resumes (versions,
master, encrypted file in its own table), job preferences, saved search
profiles (unified Keywords & Skills), canonical jobs with every source copy,
and the per-user job state that stops seen jobs being shown again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False)]


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("headline", sa.String(255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("applicant_info", sa.JSON(), nullable=False),
        sa.Column("work_authorization", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", name="uq_profiles_user"),
    )

    op.create_table(
        "resumes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_master", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("parsed", sa.JSON(), nullable=True),
        sa.Column("parse_status", sa.String(16), nullable=False),
        sa.Column("parse_error", sa.String(300), nullable=True),
        sa.Column("parsed_by", sa.String(300), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "version", name="uq_resumes_user_version"),
    )
    op.create_index("ix_resumes_user_id", "resumes", ["user_id"])

    op.create_table(
        "resume_files",
        sa.Column("resume_id", sa.String(36), sa.ForeignKey("resumes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
    )

    op.create_table(
        "job_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("titles", sa.JSON(), nullable=False),
        sa.Column("seniority", sa.JSON(), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("work_modes", sa.JSON(), nullable=False),
        sa.Column("job_types", sa.JSON(), nullable=False),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(8), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("avoid_companies", sa.JSON(), nullable=False),
        sa.Column("posted_within_hours", sa.Integer(), nullable=True),
        sa.Column("languages", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", name="uq_job_preferences_user"),
    )

    op.create_table(
        "search_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("titles", sa.JSON(), nullable=False),
        sa.Column("must_have", sa.JSON(), nullable=False),
        sa.Column("nice_to_have", sa.JSON(), nullable=False),
        sa.Column("exclude", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "name", name="uq_search_profiles_user_name"),
    )
    op.create_index("ix_search_profiles_user_id", "search_profiles", ["user_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_key", sa.String(700), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("company", sa.String(255), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("posted", sa.String(64), nullable=False),
        sa.Column("best_source", sa.String(100), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_key", name="uq_jobs_job_key"),
    )

    op.create_table(
        "job_sources_seen",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "source", name="uq_job_sources_seen_job_source"),
    )

    op.create_table(
        "user_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("keyword_score", sa.Float(), nullable=True),
        sa.Column("llm_score", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("profile_hash", sa.String(32), nullable=True),
        sa.Column("search_profile_id", sa.String(36), nullable=True),
        sa.Column("last_run_id", sa.String(36), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "job_id", name="uq_user_jobs_user_job"),
    )
    op.create_index("ix_user_jobs_user_state", "user_jobs", ["user_id", "state"])


def downgrade() -> None:
    op.drop_table("user_jobs")
    op.drop_table("job_sources_seen")
    op.drop_table("jobs")
    op.drop_table("search_profiles")
    op.drop_table("job_preferences")
    op.drop_table("resume_files")
    op.drop_table("resumes")
    op.drop_table("profiles")
