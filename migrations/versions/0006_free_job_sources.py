"""Free job sources (P3.1): catalog rows for JobSpy's Glassdoor/Google,
the keyed aggregators (Adzuna, Jooble, Careerjet) and the keyless remote
feeds; `provider_catalog.default_enabled` (sources that start switched off
for users); `jobs.best_source_rank` for the dedup rule LinkedIn > company
ATS > board > aggregator. Naukri becomes manual import only.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NEW_KEYS = ["glassdoor", "google_jobs", "adzuna", "jooble", "careerjet",
            "remoteok", "remotive", "himalayas", "jobicy", "arbeitnow", "themuse", "wwr"]


def upgrade() -> None:
    with op.batch_alter_table("provider_catalog") as batch:
        batch.add_column(sa.Column("default_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("best_source_rank", sa.Integer(), nullable=False, server_default="2"))
    op.execute("UPDATE jobs SET best_source_rank = 0 WHERE lower(best_source) = 'linkedin'")

    op.execute("UPDATE provider_catalog SET tos_risk = 'none', "
               "description = 'Jobs you paste in. Naukri blocks automated search (captcha), so there is no "
               "free automatic Naukri source; use Naukri via Apify for that.' WHERE key = 'naukri'")

    catalog = sa.table(
        "provider_catalog",
        sa.column("key", sa.String), sa.column("name", sa.String), sa.column("tier", sa.String),
        sa.column("requires_key", sa.Boolean), sa.column("key_scope", sa.String), sa.column("tos_risk", sa.String),
        sa.column("rate_limit", sa.String), sa.column("description", sa.String),
        sa.column("enabled_global", sa.Boolean), sa.column("default_enabled", sa.Boolean),
        sa.column("consecutive_failures", sa.Integer), sa.column("updated_at", sa.DateTime),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = {"enabled_global": True, "default_enabled": True, "consecutive_failures": 0, "updated_at": now,
            "tier": "free", "requires_key": False, "key_scope": None, "rate_limit": None}
    feed_rate = "one download per hour (cached)"
    op.bulk_insert(catalog, [
        {**base, "key": "glassdoor", "name": "Glassdoor", "tos_risk": "high", "default_enabled": False,
         "description": "Public job search via JobSpy (scraping). Off by default."},
        {**base, "key": "google_jobs", "name": "Google Jobs", "tos_risk": "high", "default_enabled": False,
         "description": "Google's job listings via JobSpy (scraping); often returns nothing. Off by default."},
        {**base, "key": "adzuna", "name": "Adzuna", "tier": "free_key", "requires_key": True, "key_scope": "adzuna",
         "tos_risk": "none", "rate_limit": "daily cap per user",
         "description": "Official API, covers India. Free key at developer.adzuna.com — save it as APP_ID:APP_KEY. "
                        "Returns short descriptions."},
        {**base, "key": "jooble", "name": "Jooble", "tier": "free_key", "requires_key": True, "key_scope": "jooble",
         "tos_risk": "none", "rate_limit": "daily cap per user",
         "description": "Official API, covers India. Free key at jooble.org/api/about. Returns short descriptions."},
        {**base, "key": "careerjet", "name": "Careerjet", "tier": "free_key", "requires_key": True,
         "key_scope": "careerjet", "tos_risk": "none", "rate_limit": "daily cap per user",
         "description": "Official API (en_IN for India). Free publisher key at careerjet.com/partners. "
                        "Returns short descriptions."},
        {**base, "key": "remoteok", "name": "RemoteOK", "tos_risk": "low", "rate_limit": feed_rate,
         "description": "Remote jobs feed. Runs when you search for remote work. Jobs link back to RemoteOK."},
        {**base, "key": "remotive", "name": "Remotive", "tos_risk": "low", "rate_limit": feed_rate,
         "description": "Remote jobs feed. Runs when you search for remote work."},
        {**base, "key": "himalayas", "name": "Himalayas", "tos_risk": "low", "rate_limit": feed_rate,
         "description": "Remote jobs feed. Runs when you search for remote work."},
        {**base, "key": "jobicy", "name": "Jobicy", "tos_risk": "low", "rate_limit": feed_rate,
         "description": "Remote jobs feed. Runs when you search for remote work."},
        {**base, "key": "arbeitnow", "name": "Arbeitnow", "tos_risk": "low", "rate_limit": feed_rate,
         "default_enabled": False,
         "description": "Remote jobs feed, mostly Germany/EU roles. Off by default."},
        {**base, "key": "themuse", "name": "The Muse", "tos_risk": "low", "rate_limit": feed_rate,
         "default_enabled": False,
         "description": "Remote/flexible jobs feed, mostly US roles. Off by default: as of 2026-10-16 it "
                        "refuses requests that identify as a bot (HTTP 403), and we don't disguise ours."},
        {**base, "key": "wwr", "name": "We Work Remotely", "tos_risk": "low", "rate_limit": feed_rate,
         "description": "Remote jobs RSS feed. Runs when you search for remote work."},
    ])


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k in NEW_KEYS)
    op.execute(f"DELETE FROM user_provider_settings WHERE provider_key IN ({keys})")
    op.execute(f"DELETE FROM provider_catalog WHERE key IN ({keys})")
    op.execute("UPDATE provider_catalog SET tos_risk = 'high', "
               "description = 'Manual import, or JobSpy when automatic Naukri search is on.' WHERE key = 'naukri'")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("best_source_rank")
    with op.batch_alter_table("provider_catalog") as batch:
        batch.drop_column("default_enabled")
