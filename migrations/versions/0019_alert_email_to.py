"""Job alerts: where the digest goes.

job_alerts.email_to: NULL = the sign-in email (as before), otherwise one of the user's connected Gmail
addresses (checked when the alert is saved, and again before each send).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_alerts") as batch:
        batch.add_column(sa.Column("email_to", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("job_alerts") as batch:
        batch.drop_column("email_to")
