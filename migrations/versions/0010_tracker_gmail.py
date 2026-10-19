"""Tracker & Gmail (P5): email_accounts (per-user Gmail, refresh token sealed),
email_events (one row per job-related message: snippet + extracted fields, never
the body), email_jobs (job links parsed from job-alert emails), the
"Job alert emails" search source, and application_status_events.email_event_id
(which email caused a status change).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gmail_address", sa.String(255), nullable=False),
        sa.Column("token_sealed", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_history_id", sa.String(32), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "gmail_address", name="uq_email_accounts_user_address"),
    )
    op.create_index("ix_email_accounts_user_id", "email_accounts", ["user_id"])
    op.create_index("ix_email_accounts_due", "email_accounts", ["status", "next_sync_at"])

    op.create_table(
        "email_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("email_accounts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=True),
        sa.Column("sender", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("snippet", sa.String(300), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(24), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("role", sa.String(255), nullable=True),
        sa.Column("summary", sa.String(300), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("applied_status", sa.String(32), nullable=True),
        sa.Column("match_score", sa.Integer(), nullable=True),
        sa.Column("suggestions", sa.JSON(), nullable=False),
        sa.Column("engine", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("account_id", "message_id", name="uq_email_events_account_message"),
    )
    op.create_index("ix_email_events_user_state", "email_events", ["user_id", "state"])
    op.create_index("ix_email_events_thread", "email_events", ["user_id", "thread_id"])
    op.create_index("ix_email_events_created_at", "email_events", ["created_at"])

    op.create_table(
        "email_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("email_accounts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("company", sa.String(255), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "url", name="uq_email_jobs_user_url"),
    )
    op.create_index("ix_email_jobs_user_received", "email_jobs", ["user_id", "received_at"])

    with op.batch_alter_table("application_status_events") as batch:
        batch.add_column(sa.Column("email_event_id", sa.String(36), nullable=True))

    catalog = sa.table(
        "provider_catalog",
        sa.column("key", sa.String), sa.column("name", sa.String), sa.column("tier", sa.String),
        sa.column("requires_key", sa.Boolean), sa.column("key_scope", sa.String), sa.column("tos_risk", sa.String),
        sa.column("rate_limit", sa.String), sa.column("description", sa.String),
        sa.column("enabled_global", sa.Boolean), sa.column("default_enabled", sa.Boolean),
        sa.column("consecutive_failures", sa.Integer), sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(catalog, [{
        "key": "email_alerts", "name": "Job alert emails", "tier": "free", "requires_key": False,
        "key_scope": None, "tos_risk": "none", "rate_limit": "your own inbox, read on each Gmail sync",
        "enabled_global": True, "default_enabled": True, "consecutive_failures": 0,
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "description": "Jobs from the job-alert emails in your connected Gmail (Naukri, LinkedIn, Indeed, "
                       "Foundit, Instahyre). The free way to get Naukri jobs: turn on Naukri job alerts, "
                       "connect Gmail, and they arrive here. Nothing runs until Gmail is connected.",
    }])


def downgrade() -> None:
    op.execute("DELETE FROM user_provider_settings WHERE provider_key = 'email_alerts'")
    op.execute("DELETE FROM provider_catalog WHERE key = 'email_alerts'")
    with op.batch_alter_table("application_status_events") as batch:
        batch.drop_column("email_event_id")
    op.drop_table("email_jobs")
    op.drop_table("email_events")
    op.drop_table("email_accounts")
