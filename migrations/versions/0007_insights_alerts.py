"""Match insights, freshness, job alerts, more sources (P3.2): user_jobs.breakdown;
profiles.eeo_encrypted (sealed diversity answers);
jobs.posted_at (backfilled) and LinkedIn applicant counts; job_alerts and
job_alert_runs; catalog rows for HN "Who is hiring?" and Google Jobs via the
user's own free key (Bright Data / SerpApi / JSearch); The Muse takes a key.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _parse(value: str):
    """Same rules as providers.job_sources.base.parse_date (kept local: migrations don't import app code)."""
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        n = float(text)
        return datetime.fromtimestamp(n / 1000 if n > 10_000_000_000 else n, tz=timezone.utc)
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00").replace(" UTC", "+00:00")),
                  parsedate_to_datetime):
        try:
            dt = parse(text)
        except (ValueError, TypeError, IndexError):
            continue
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    return None


def upgrade() -> None:
    with op.batch_alter_table("user_jobs") as batch:
        batch.add_column(sa.Column("breakdown", sa.JSON(), nullable=True))
    with op.batch_alter_table("profiles") as batch:
        batch.add_column(sa.Column("eeo_encrypted", sa.Text(), nullable=True))
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("posted_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("applicants", sa.String(32), nullable=True))
        batch.add_column(sa.Column("applicants_checked_at", sa.DateTime(), nullable=True))

    conn = op.get_bind()
    jobs = sa.table("jobs", sa.column("id", sa.String), sa.column("posted", sa.String),
                    sa.column("posted_at", sa.DateTime))
    for row in conn.execute(sa.select(jobs.c.id, jobs.c.posted).where(jobs.c.posted != "")).fetchall():
        parsed = _parse(row.posted)
        if parsed:
            conn.execute(jobs.update().where(jobs.c.id == row.id).values(posted_at=parsed))

    op.create_table(
        "job_alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("search_profile_id", sa.String(36), sa.ForeignKey("search_profiles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("hour_local", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("min_score", sa.Integer(), nullable=False),
        sa.Column("email_digest", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_job_alerts_user_id", "job_alerts", ["user_id"])
    op.create_index("ix_job_alerts_due", "job_alerts", ["enabled", "next_run_at"])
    op.create_table(
        "job_alert_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alert_id", sa.String(36), sa.ForeignKey("job_alerts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("job_ids", sa.JSON(), nullable=False),
        sa.Column("emailed_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_job_alert_runs_alert_id", "job_alert_runs", ["alert_id"])
    op.create_index("ix_job_alert_runs_user_id", "job_alert_runs", ["user_id"])

    catalog = sa.table(
        "provider_catalog",
        sa.column("key", sa.String), sa.column("name", sa.String), sa.column("tier", sa.String),
        sa.column("requires_key", sa.Boolean), sa.column("key_scope", sa.String), sa.column("tos_risk", sa.String),
        sa.column("rate_limit", sa.String), sa.column("description", sa.String),
        sa.column("enabled_global", sa.Boolean), sa.column("default_enabled", sa.Boolean),
        sa.column("consecutive_failures", sa.Integer), sa.column("updated_at", sa.DateTime),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.bulk_insert(catalog, [
        {"key": "hn_hiring", "name": "HN Who is hiring", "tier": "free", "requires_key": False, "key_scope": None,
         "tos_risk": "none", "rate_limit": "one download per 6 hours (cached)", "enabled_global": True,
         "default_enabled": True, "consecutive_failures": 0, "updated_at": now,
         "description": "Monthly Hacker News 'Who is hiring?' thread (public API). Runs on remote searches; "
                        "mostly US/EU startups, filtered by region."},
        {"key": "google_jobs_api", "name": "Google Jobs (your key)", "tier": "free_key", "requires_key": True,
         "key_scope": "brightdata,serpapi,jsearch", "tos_risk": "none", "rate_limit": "daily cap per user",
         "enabled_global": True, "default_enabled": False, "consecutive_failures": 0, "updated_at": now,
         "description": "Google Jobs listings through YOUR OWN free key, tried in order: Bright Data "
                        "(save as API_KEY:ZONE; ~5,000/month free; experimental), SerpApi (250/month free), "
                        "JSearch on RapidAPI (free Basic plan). Never uses a shared key. Off by default."},
    ])
    op.execute("UPDATE provider_catalog SET key_scope = 'themuse', description = 'Remote/flexible jobs feed, "
               "mostly US roles. Off by default: as of 2026-10-16 it refuses requests that identify as a bot "
               "(HTTP 403). Uses the platform''s free The Muse key (scope themuse) when one is saved; stays off "
               "if that does not help. We never disguise requests.' WHERE key = 'themuse'")


def downgrade() -> None:
    op.execute("DELETE FROM user_provider_settings WHERE provider_key IN ('hn_hiring', 'google_jobs_api')")
    op.execute("DELETE FROM provider_catalog WHERE key IN ('hn_hiring', 'google_jobs_api')")
    op.execute("UPDATE provider_catalog SET key_scope = NULL WHERE key = 'themuse'")
    op.drop_table("job_alert_runs")
    op.drop_table("job_alerts")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("applicants_checked_at")
        batch.drop_column("applicants")
        batch.drop_column("posted_at")
    with op.batch_alter_table("user_jobs") as batch:
        batch.drop_column("breakdown")
    with op.batch_alter_table("profiles") as batch:
        batch.drop_column("eeo_encrypted")
