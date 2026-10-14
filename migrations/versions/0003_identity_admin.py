"""Identity & admin (Phase 1): user roles/verification/session revocation,
per-user entitlements, append-only audit log, global feature flags.

hashed_password becomes nullable: OTP and Google users have no password.
batch_alter_table so the same migration runs on SQLite (tests) and
Postgres (Supabase).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("hashed_password", existing_type=sa.String(), nullable=True)
        batch.add_column(sa.Column("role", sa.String(16), nullable=False, server_default="user"))
        batch.add_column(sa.Column("manager_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("auth_provider", sa.String(16), nullable=False, server_default="password"))
        batch.add_column(sa.Column("google_sub", sa.String(255), nullable=True))
        batch.add_column(sa.Column("email_verified_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("sessions_revoked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key("fk_users_manager_id", "users", ["manager_id"], ["id"])
        batch.create_unique_constraint("uq_users_google_sub", ["google_sub"])
        batch.create_index("ix_users_manager_id", ["manager_id"])

    op.create_table(
        "user_entitlements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("quota_per_day", sa.Integer(), nullable=True),
        sa.Column("max_level", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "feature", name="uq_entitlement_user_feature"),
    )
    op.create_index("ix_user_entitlements_user_id", "user_entitlements", ["user_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_target_id", "audit_logs", ["target_id"])
    op.create_index("ix_audit_logs_at", "audit_logs", ["at"])

    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("feature_flags")
    op.drop_table("audit_logs")
    op.drop_table("user_entitlements")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_manager_id")
        batch.drop_constraint("uq_users_google_sub", type_="unique")
        batch.drop_constraint("fk_users_manager_id", type_="foreignkey")
        for col in ("session_version", "sessions_revoked_at", "last_login_at", "email_verified_at", "google_sub",
                    "auth_provider", "manager_id", "role"):
            batch.drop_column(col)
        batch.alter_column("hashed_password", existing_type=sa.String(), nullable=False)
