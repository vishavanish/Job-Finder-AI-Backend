"""Apply Center, part B (P4b): apply_runs (one run per job through the apply
engine: auto = level-2 review & submit on Greenhouse/Lever, fast = LinkedIn
Easy Apply prefill on the user's own PC); apply_proofs (confirmation
screenshot, envelope-encrypted, deleted after APPLY_PROOF_RETENTION_DAYS);
apply_settings (per-user autopilot level, risk notice, apply hours, pause).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apply_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_key", sa.String(700), nullable=False),
        sa.Column("company_title", sa.String(800), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(16), nullable=False),
        sa.Column("apply_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        # Set while the run is active, NULL once it ends: unique, so one job has one active run.
        sa.Column("active_key", sa.String(800), nullable=True),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("resume_id", sa.String(36), sa.ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("apply_packs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("active_key", name="uq_apply_runs_active_key"),
    )
    op.create_index("ix_apply_runs_user_state", "apply_runs", ["user_id", "state"])
    op.create_index("ix_apply_runs_user_submitted", "apply_runs", ["user_id", "submitted_at"])

    op.create_table(
        "apply_proofs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("apply_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("confirmation_text", sa.Text(), nullable=True),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_apply_proofs_run_id", "apply_proofs", ["run_id"])
    op.create_index("ix_apply_proofs_created_at", "apply_proofs", ["created_at"])

    op.create_table(
        "apply_settings",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("risk_accepted_at", sa.DateTime(), nullable=True),
        sa.Column("hours_start", sa.Integer(), nullable=False),
        sa.Column("hours_end", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("apply_settings")
    op.drop_table("apply_proofs")
    op.drop_table("apply_runs")
