"""Browser extension "Job Finder capture" (P8a E1).

- extension_tokens: capture-only tokens (SHA-256 only), revocable.
- captured_jobs: jobs the user saved from pages they opened; unique per (user, url).
- provider_catalog row `browser_capture` ("Saved from your browser", kind capture): free, on by default,
  available once an extension is connected.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-31
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extension_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_extension_tokens_user_id", "extension_tokens", ["user_id"])
    op.create_table(
        "captured_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("company", sa.String(255), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("posted", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "url", name="uq_captured_jobs_user_url"),
    )
    op.create_index("ix_captured_jobs_user_id", "captured_jobs", ["user_id"])
    op.create_index("ix_captured_jobs_captured_at", "captured_jobs", ["captured_at"])

    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM provider_catalog WHERE key = 'browser_capture'")).first() is None:
        catalog = sa.table(
            "provider_catalog",
            sa.column("key", sa.String), sa.column("name", sa.String), sa.column("tier", sa.String),
            sa.column("requires_key", sa.Boolean), sa.column("key_scope", sa.String), sa.column("tos_risk", sa.String),
            sa.column("rate_limit", sa.String), sa.column("enabled_global", sa.Boolean),
            sa.column("default_enabled", sa.Boolean), sa.column("consecutive_failures", sa.Integer),
            sa.column("updated_at", sa.DateTime), sa.column("description", sa.String),
        )
        op.bulk_insert(catalog, [{
            "key": "browser_capture", "name": "Saved from your browser", "tier": "free", "requires_key": False,
            "key_scope": None, "tos_risk": "low", "rate_limit": "your own clicks", "enabled_global": True,
            "default_enabled": True, "consecutive_failures": 0,
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "description": "Jobs you saved with the Job Finder browser extension from pages you opened "
                           "(Naukri, Foundit, Glassdoor, LinkedIn, Indeed). Needs the extension connected.",
        }])


def downgrade() -> None:
    op.execute("DELETE FROM user_provider_settings WHERE provider_key = 'browser_capture'")
    op.execute("DELETE FROM provider_catalog WHERE key = 'browser_capture'")
    op.drop_index("ix_captured_jobs_captured_at", table_name="captured_jobs")
    op.drop_index("ix_captured_jobs_user_id", table_name="captured_jobs")
    op.drop_table("captured_jobs")
    op.drop_index("ix_extension_tokens_user_id", table_name="extension_tokens")
    op.drop_table("extension_tokens")
