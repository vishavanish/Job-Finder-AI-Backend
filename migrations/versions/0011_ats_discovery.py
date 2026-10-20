"""ATS Discovery (P6): ats_companies (shared company registry), ats_jobs (open
jobs index; short rows, no full descriptions), ats_platforms (admin on/off +
health per ATS platform), and the "Company career pages" search source.

On Postgres, ats_jobs also gets a generated `tsv` column (title A, department B,
snippet C) with a GIN index for the title search; SQLite (dev/tests) has none.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

PLATFORMS = ("greenhouse", "lever", "workday", "recruitee", "ashby", "smartrecruiters", "workable", "personio",
             "breezy", "teamtailor", "rippling", "gem")


def upgrade() -> None:
    op.create_table(
        "ats_companies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ref_key", sa.String(400), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("site", sa.String(128), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("careers_url", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("added_by_user_id", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("job_count", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(255), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("last_crawled_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("next_crawl_at", sa.DateTime(), nullable=True),
        sa.Column("hot_until", sa.DateTime(), nullable=True),
        sa.Column("fail_count", sa.Integer(), nullable=False),
        sa.Column("gone_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ref_key", name="uq_ats_companies_ref_key"),
    )
    op.create_index("ix_ats_companies_due", "ats_companies", ["active", "next_crawl_at"])
    op.create_index("ix_ats_companies_platform", "ats_companies", ["platform"])

    op.create_table(
        "ats_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("ats_companies.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("title_norm", sa.String(500), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("country", sa.String(64), nullable=False),
        sa.Column("remote", sa.Boolean(), nullable=False),
        sa.Column("department", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("snippet", sa.String(300), nullable=False),
        sa.Column("job_key", sa.String(700), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("company_id", "external_id", name="uq_ats_jobs_company_external"),
    )
    op.create_index("ix_ats_jobs_company_id", "ats_jobs", ["company_id"])
    op.create_index("ix_ats_jobs_open_posted", "ats_jobs", ["closed_at", "posted_at"])
    op.create_index("ix_ats_jobs_job_key", "ats_jobs", ["job_key"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE ats_jobs ADD COLUMN tsv tsvector GENERATED ALWAYS AS ("
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(department, '')), 'B') || "
            "setweight(to_tsvector('english', coalesce(snippet, '')), 'C')) STORED")
        op.execute("CREATE INDEX ix_ats_jobs_tsv ON ats_jobs USING GIN (tsv)")

    op.create_table(
        "ats_platforms",
        sa.Column("platform", sa.String(24), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
    )
    platforms = sa.table("ats_platforms", sa.column("platform", sa.String), sa.column("enabled", sa.Boolean),
                         sa.column("consecutive_failures", sa.Integer))
    op.bulk_insert(platforms, [{"platform": p, "enabled": True, "consecutive_failures": 0} for p in PLATFORMS])

    catalog = sa.table(
        "provider_catalog",
        sa.column("key", sa.String), sa.column("name", sa.String), sa.column("tier", sa.String),
        sa.column("requires_key", sa.Boolean), sa.column("key_scope", sa.String), sa.column("tos_risk", sa.String),
        sa.column("rate_limit", sa.String), sa.column("description", sa.String),
        sa.column("enabled_global", sa.Boolean), sa.column("default_enabled", sa.Boolean),
        sa.column("consecutive_failures", sa.Integer), sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(catalog, [{
        "key": "company_ats", "name": "Company career pages", "tier": "free", "requires_key": False,
        "key_scope": None, "tos_risk": "none", "rate_limit": "read from our index of company career pages",
        "enabled_global": True, "default_enabled": True, "consecutive_failures": 0,
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "description": "Jobs straight from company career pages (Greenhouse, Lever, Workday, Recruitee, Ashby, "
                       "SmartRecruiters, Workable, Personio, Breezy, Teamtailor, Rippling, Gem). "
                       "Searched in our index, refreshed every 6-24 hours; Auto apply works for Greenhouse "
                       "and Lever jobs.",
    }])


def downgrade() -> None:
    op.execute("DELETE FROM user_provider_settings WHERE provider_key = 'company_ats'")
    op.execute("DELETE FROM provider_catalog WHERE key = 'company_ats'")
    op.drop_table("ats_platforms")
    op.drop_table("ats_jobs")
    op.drop_table("ats_companies")
