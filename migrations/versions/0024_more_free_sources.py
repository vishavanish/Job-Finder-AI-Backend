"""More free job sources (QA7 decisions 1, 2, 4): Working Nomads, Findwork.dev; HN job posts join hn_hiring.

- `working_nomads`: keyless remote feed, on by default (runs on remote searches, like RemoteOK).
- `findwork`: Findwork.dev keyword API on a free key (scope `findwork`, `.env` FINDWORK_API_KEY), off by default.
- `hn_hiring` now also reads the YC companies' "Is hiring" posts; its description says so.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-31
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

HN_OLD = ("Monthly Hacker News 'Who is hiring?' thread (public API). Runs on remote searches; "
          "mostly US/EU startups, filtered by region.")
HN_NEW = ("Monthly Hacker News 'Who is hiring?' thread plus the YC startups' own 'is hiring' posts (public API). "
          "Runs on remote searches; mostly US/EU startups, filtered by region.")


def upgrade() -> None:
    conn = op.get_bind()
    catalog = sa.table(
        "provider_catalog",
        sa.column("key", sa.String), sa.column("name", sa.String), sa.column("tier", sa.String),
        sa.column("requires_key", sa.Boolean), sa.column("key_scope", sa.String), sa.column("tos_risk", sa.String),
        sa.column("rate_limit", sa.String), sa.column("enabled_global", sa.Boolean),
        sa.column("default_enabled", sa.Boolean), sa.column("consecutive_failures", sa.Integer),
        sa.column("updated_at", sa.DateTime), sa.column("description", sa.String),
    )
    have = {k for (k,) in conn.execute(sa.text("SELECT key FROM provider_catalog"))}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        {"key": "working_nomads", "name": "Working Nomads", "tier": "free", "requires_key": False, "key_scope": None,
         "tos_risk": "none", "rate_limit": "one download per hour (cached)", "enabled_global": True,
         "default_enabled": True, "consecutive_failures": 0, "updated_at": now,
         "description": "Working Nomads' public remote-job export (~40 recent jobs, full descriptions). "
                        "Runs on remote searches; filtered by region."},
        {"key": "findwork", "name": "Findwork", "tier": "free_key", "requires_key": True, "key_scope": "findwork",
         "tos_risk": "none", "rate_limit": "daily cap per user", "enabled_global": True, "default_enabled": False,
         "consecutive_failures": 0, "updated_at": now,
         "description": "Findwork.dev tech-jobs API with a free key (findwork.dev/developers). Mostly US/EU and "
                        "remote roles."},
    ]
    new = [r for r in rows if r["key"] not in have]
    if new:
        op.bulk_insert(catalog, new)
    conn.execute(sa.text("UPDATE provider_catalog SET description = :new WHERE key = 'hn_hiring' AND description = :old"),
                 {"new": HN_NEW, "old": HN_OLD})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM user_provider_settings WHERE provider_key IN ('working_nomads', 'findwork')"))
    conn.execute(sa.text("DELETE FROM provider_catalog WHERE key IN ('working_nomads', 'findwork')"))
    conn.execute(sa.text("UPDATE provider_catalog SET description = :old WHERE key = 'hn_hiring' AND description = :new"),
                 {"new": HN_NEW, "old": HN_OLD})
