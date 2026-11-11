"""Resume check (QA10).

- resumes.ats_check: the latest check result (JSON: version, score, band, findings, pages, words, seen_text).
- resumes.ats_checked_at: when it ran.

Both nullable; old resumes are checked the first time they are listed or opened.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resumes") as batch:
        batch.add_column(sa.Column("ats_check", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("ats_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("resumes") as batch:
        batch.drop_column("ats_checked_at")
        batch.drop_column("ats_check")
