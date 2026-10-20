"""Apply Center, part A (P4a): jobs.apply_url (the company's own apply link
behind a LinkedIn/Indeed posting); answer_bank (screening answers, AI drafts
need the user's approval); apply_packs (cached prep pack per job: tailored
resume, cover letter, email draft).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("apply_url", sa.Text(), nullable=True))

    op.create_table(
        "answer_bank",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_hash", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("field_type", sa.String(16), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "question_hash", name="uq_answer_bank_user_question"),
    )
    op.create_index("ix_answer_bank_user_id", "answer_bank", ["user_id"])

    op.create_table(
        "apply_packs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("master_resume_id", sa.String(36), sa.ForeignKey("resumes.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("master_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("want_resume", sa.Boolean(), nullable=False),
        sa.Column("want_cover_letter", sa.Boolean(), nullable=False),
        sa.Column("tailored_resume_id", sa.String(36), sa.ForeignKey("resumes.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("cover_letter", sa.Text(), nullable=True),
        sa.Column("email_to", sa.String(255), nullable=True),
        sa.Column("email_subject", sa.String(300), nullable=True),
        sa.Column("email_body", sa.Text(), nullable=True),
        sa.Column("dropped", sa.JSON(), nullable=False),
        sa.Column("engine", sa.String(300), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "job_id", name="uq_apply_packs_user_job"),
    )
    op.create_index("ix_apply_packs_user_id", "apply_packs", ["user_id"])


def downgrade() -> None:
    op.drop_table("apply_packs")
    op.drop_table("answer_bank")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("apply_url")
