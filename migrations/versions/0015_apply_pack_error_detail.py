"""apply_packs.error_detail: the model-by-model reason a prep pack failed.

`error` now holds a short message for people; the technical detail (which
models were tried and why each failed) moves here and is shown folded
(QA1 §7).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("apply_packs") as batch:
        batch.add_column(sa.Column("error_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("apply_packs") as batch:
        batch.drop_column("error_detail")
