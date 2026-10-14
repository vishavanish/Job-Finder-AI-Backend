"""Cross-source application matching: applications.job_key + alias_urls.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("job_key", sa.String(600), nullable=True))
    op.add_column("applications", sa.Column("alias_urls", sa.JSON(), nullable=True))
    op.create_index("ix_application_user_job_key", "applications", ["user_id", "job_key"])


def downgrade() -> None:
    op.drop_index("ix_application_user_job_key", table_name="applications")
    op.drop_column("applications", "alias_urls")
    op.drop_column("applications", "job_key")
