"""Models & providers (Phase 2): AI model registry, per-task defaults and
user choices, encrypted key store (platform + BYOK), usage ledger, job-source
catalog with per-user on/off, and the user's "free tools only" switch.

Seeds today's three scoring models (Gemini -> Groq -> HF, same order as the
old hardcoded chain; their keys still come from .env until an admin stores
one) and today's job sources.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("free_only_mode", sa.Boolean(), nullable=False, server_default=sa.false()))

    ai_models = op.create_table(
        "ai_models",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=True),
        sa.Column("key_scope", sa.String(64), nullable=False),
        sa.Column("cost_per_1k_in", sa.Float(), nullable=False),
        sa.Column("cost_per_1k_out", sa.Float(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("enabled_global", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", "model_id", "base_url", name="uq_ai_models_provider_model"),
    )

    op.create_table(
        "task_model_defaults",
        sa.Column("task", sa.String(32), primary_key=True),
        sa.Column("model_ref", sa.String(36), sa.ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "user_model_prefs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("model_ref", sa.String(36), sa.ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "task", name="uq_user_model_pref"),
    )
    op.create_index("ix_user_model_prefs_user_id", "user_model_prefs", ["user_id"])

    op.create_table(
        "secrets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_type", sa.String(8), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_secrets_user_id", "secrets", ["user_id"])
    op.create_index("ix_secrets_owner_scope", "secrets", ["owner_type", "scope"])

    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("model_ref", sa.String(36), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("key_source", sa.String(16), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("ref", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_usage_ledger_user_id", "usage_ledger", ["user_id"])
    op.create_index("ix_usage_ledger_model_ref", "usage_ledger", ["model_ref"])
    op.create_index("ix_usage_ledger_created_at", "usage_ledger", ["created_at"])

    catalog = op.create_table(
        "provider_catalog",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.Column("requires_key", sa.Boolean(), nullable=False),
        sa.Column("key_scope", sa.String(64), nullable=True),
        sa.Column("tos_risk", sa.String(8), nullable=False),
        sa.Column("rate_limit", sa.String(100), nullable=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("enabled_global", sa.Boolean(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "user_provider_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider_key", sa.String(32), sa.ForeignKey("provider_catalog.key", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "provider_key", name="uq_user_provider_setting"),
    )
    op.create_index("ix_user_provider_settings_user_id", "user_provider_settings", ["user_id"])

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = {"tier": "free", "tasks": [], "base_url": None, "cost_per_1k_in": 0.0, "cost_per_1k_out": 0.0,
            "context_window": None, "max_output_tokens": None, "enabled_global": True, "notes": None,
            "consecutive_failures": 0, "created_by": None, "created_at": now, "updated_at": now}
    op.bulk_insert(ai_models, [
        {**base, "id": str(uuid.uuid4()), "provider": "gemini", "model_id": "gemini-2.5-flash",
         "display_name": "Gemini 2.5 Flash", "key_scope": "gemini", "priority": 10,
         "notes": "Free tier with rate limits."},
        {**base, "id": str(uuid.uuid4()), "provider": "groq", "model_id": "openai/gpt-oss-120b",
         "display_name": "GPT-OSS 120B (Groq)", "key_scope": "groq", "priority": 20,
         "notes": "Free tier with rate limits."},
        {**base, "id": str(uuid.uuid4()), "provider": "huggingface", "model_id": "Qwen/Qwen3-8B",
         "display_name": "Qwen3 8B (Hugging Face)", "key_scope": "huggingface", "priority": 30,
         "max_output_tokens": 2000, "notes": "Free inference credits."},
    ])

    src = {"rate_limit": None, "enabled_global": True, "consecutive_failures": 0, "updated_at": now}
    op.bulk_insert(catalog, [
        {**src, "key": "linkedin", "name": "LinkedIn", "tier": "free", "requires_key": False, "key_scope": None,
         "tos_risk": "high", "description": "Public job search via JobSpy (scraping). Paced with retries."},
        {**src, "key": "indeed", "name": "Indeed", "tier": "free", "requires_key": False, "key_scope": None,
         "tos_risk": "high", "description": "Public job search via JobSpy (scraping)."},
        {**src, "key": "naukri", "name": "Naukri", "tier": "free", "requires_key": False, "key_scope": None,
         "tos_risk": "high", "description": "Manual import, or JobSpy when automatic Naukri search is on."},
        {**src, "key": "naukri_apify", "name": "Naukri via Apify", "tier": "paid", "requires_key": True,
         "key_scope": "apify", "tos_risk": "low", "description": "Apify actor; uses Apify credits."},
    ])


def downgrade() -> None:
    op.drop_table("user_provider_settings")
    op.drop_table("provider_catalog")
    op.drop_table("usage_ledger")
    op.drop_table("secrets")
    op.drop_table("user_model_prefs")
    op.drop_table("task_model_defaults")
    op.drop_table("ai_models")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("free_only_mode")
