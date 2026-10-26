"""Target companies (QA4): follow companies and get told when they post a matching role.

ats_follows (a user follows a registry company, or a name still being looked for), ats_follow_hits (new
matching roles until seen / emailed), ats_follow_settings (alert mode, titles, places per user) and
ats_companies.follower_count (followed companies refresh every ATS_FOLLOWED_REFRESH_HOURS).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ats_companies") as batch:
        batch.add_column(sa.Column("follower_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "ats_follows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("ats_companies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("name_norm", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="following"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "company_id", name="uq_ats_follows_user_company"),
    )
    op.create_index("ix_ats_follows_user_id", "ats_follows", ["user_id"])
    op.create_index("ix_ats_follows_company_id", "ats_follows", ["company_id"])
    op.create_index("ix_ats_follows_name_norm", "ats_follows", ["name_norm"])
    op.create_table(
        "ats_follow_hits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("ats_companies.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("ats_job_id", sa.String(36), sa.ForeignKey("ats_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("emailed_at", sa.DateTime(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "ats_job_id", name="uq_ats_follow_hits_user_job"),
    )
    op.create_index("ix_ats_follow_hits_user_id", "ats_follow_hits", ["user_id"])
    op.create_table(
        "ats_follow_settings",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("alert_mode", sa.String(16), nullable=False, server_default="daily"),
        sa.Column("titles", sa.JSON(), nullable=False),
        sa.Column("places", sa.JSON(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("last_digest_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ats_follow_settings")
    op.drop_index("ix_ats_follow_hits_user_id", table_name="ats_follow_hits")
    op.drop_table("ats_follow_hits")
    op.drop_index("ix_ats_follows_name_norm", table_name="ats_follows")
    op.drop_index("ix_ats_follows_company_id", table_name="ats_follows")
    op.drop_index("ix_ats_follows_user_id", table_name="ats_follows")
    op.drop_table("ats_follows")
    with op.batch_alter_table("ats_companies") as batch:
        batch.drop_column("follower_count")
