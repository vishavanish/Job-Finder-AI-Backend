"""ATS Discovery: Keka and Zoho Recruit (Indian career-page platforms, QA7).

Adds their `ats_platforms` rows (on) so they show in platform health and the admin on/off switches. The
companies come from the seed file: `ats_tick` loads it again when it names a platform the registry has
no company for yet.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-31
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

NEW = ("keka", "zoho_recruit")


def upgrade() -> None:
    conn = op.get_bind()
    for platform in NEW:
        if conn.execute(sa.text("SELECT 1 FROM ats_platforms WHERE platform = :p"), {"p": platform}).first() is None:
            conn.execute(sa.text("INSERT INTO ats_platforms (platform, enabled, consecutive_failures) "
                                 "VALUES (:p, :on, 0)"), {"p": platform, "on": True})


def downgrade() -> None:
    conn = op.get_bind()
    for platform in NEW:
        conn.execute(sa.text("DELETE FROM ats_platforms WHERE platform = :p"), {"p": platform})
