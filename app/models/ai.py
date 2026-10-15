"""
app/models/ai.py
------------------
Tables behind the AI model registry, key store, usage ledger and job-source
catalog (CLAUDE.md 7.4, 7.9):

  - AiModel: one row per model an admin added. `provider` picks the adapter
    in app/providers/llm/; `key_scope` names the key it uses (a native
    provider name, or e.g. "openrouter" for an OpenAI-compatible endpoint).
    Health columns feed the admin view; the live circuit breaker is in Redis.
  - TaskModelDefault: admin's default model per task type.
  - UserModelPref: a user's chosen model per task type.
  - Secret: an API key, encrypted (see app/services/key_store.py). Platform
    keys have owner_type "platform"; BYOK keys have owner_type "user".
  - UsageLedger: one row per model call attempt (tokens, latency, cost).
  - ProviderCatalog: one row per job source, with tier and health.
  - UserProviderSetting: a user's on/off choice for one job source.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class AiModel(Base):
    __tablename__ = "ai_models"
    __table_args__ = (UniqueConstraint("provider", "model_id", "base_url", name="uq_ai_models_provider_model"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    provider = Column(String(32), nullable=False)          # gemini | groq | huggingface | anthropic | openai_compatible
    model_id = Column(String(255), nullable=False)          # the provider's model name
    display_name = Column(String(255), nullable=False)
    tier = Column(String(8), nullable=False, default="free")  # free | paid
    tasks = Column(JSON, nullable=False, default=list)      # task types it may serve; [] = every text task
    base_url = Column(String(512), nullable=True)           # openai_compatible only
    key_scope = Column(String(64), nullable=False)          # which stored key it uses
    cost_per_1k_in = Column(Float, nullable=False, default=0.0)   # USD
    cost_per_1k_out = Column(Float, nullable=False, default=0.0)  # USD
    context_window = Column(Integer, nullable=True)
    max_output_tokens = Column(Integer, nullable=True)
    enabled_global = Column(Boolean, nullable=False, default=True)
    priority = Column(Integer, nullable=False, default=100)  # lower runs first in the fallback chain
    notes = Column(String(500), nullable=True)

    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error = Column(String(300), nullable=True)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class TaskModelDefault(Base):
    __tablename__ = "task_model_defaults"

    task = Column(String(32), primary_key=True)
    model_ref = Column(String(36), ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class UserModelPref(Base):
    __tablename__ = "user_model_prefs"
    __table_args__ = (UniqueConstraint("user_id", "task", name="uq_user_model_pref"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    task = Column(String(32), nullable=False)
    model_ref = Column(String(36), ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class Secret(Base):
    __tablename__ = "secrets"

    id = Column(String(36), primary_key=True, default=_uuid)
    owner_type = Column(String(8), nullable=False)          # platform | user
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    scope = Column(String(64), nullable=False)              # e.g. gemini, anthropic, openrouter, apify
    wrapped_key = Column(Text, nullable=False)              # per-secret data key, encrypted with the master key
    ciphertext = Column(Text, nullable=False)               # the API key, encrypted with the data key
    last4 = Column(String(4), nullable=False)
    label = Column(String(100), nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    last_used_at = Column(DateTime, nullable=True)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, nullable=True, index=True)     # None = system work
    task = Column(String(32), nullable=False)
    model_ref = Column(String(36), nullable=True, index=True)  # ai_models.id (kept if the model is deleted)
    provider = Column(String(32), nullable=False)
    model_id = Column(String(255), nullable=False)
    key_source = Column(String(16), nullable=False)          # byok | platform | env | request
    tokens_in = Column(Integer, nullable=False, default=0)
    tokens_out = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    success = Column(Boolean, nullable=False)
    error = Column(String(300), nullable=True)
    ref = Column(String(64), nullable=True)                  # task_id / run_id
    created_at = Column(DateTime, default=_now, nullable=False, index=True)


class ProviderCatalog(Base):
    __tablename__ = "provider_catalog"

    key = Column(String(32), primary_key=True)
    name = Column(String(100), nullable=False)
    tier = Column(String(8), nullable=False)                 # free | free_key | paid | byok
    requires_key = Column(Boolean, nullable=False, default=False)
    key_scope = Column(String(64), nullable=True)            # stored key it uses, if any
    tos_risk = Column(String(8), nullable=False, default="none")  # none | low | high
    rate_limit = Column(String(100), nullable=True)
    description = Column(String(500), nullable=True)
    enabled_global = Column(Boolean, nullable=False, default=True)

    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error = Column(String(300), nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class UserProviderSetting(Base):
    __tablename__ = "user_provider_settings"
    __table_args__ = (UniqueConstraint("user_id", "provider_key", name="uq_user_provider_setting"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    provider_key = Column(String(32), ForeignKey("provider_catalog.key", ondelete="CASCADE"), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
