"""ats_probes: what the company resolver found for a company name seen in search results (QA3 §A).

A row per normalised company name: the registry company it maps to (found on the company's website or by
a checked slug), or none (tried again after ATS_RESOLVER_RETRY_DAYS).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ats_probes",
        sa.Column("name_norm", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("ats_companies.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("via", sa.String(16), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ats_probes")
