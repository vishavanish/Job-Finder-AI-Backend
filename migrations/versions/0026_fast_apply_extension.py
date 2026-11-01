"""Fast apply in the user's own browser (P8b E3a).

- apply_settings.extension_assist: the connected browser extension may fill Fast apply forms (default on).
- apply_settings.fast_notice_at: when the user accepted the one-time Fast apply notice.

Runs filled by the extension keep `plan.runner = "extension"` and the new active state `in_browser`
(a string column, no schema change).

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("apply_settings") as batch:
        batch.add_column(sa.Column("extension_assist", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("fast_notice_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("apply_settings") as batch:
        batch.drop_column("fast_notice_at")
        batch.drop_column("extension_assist")
