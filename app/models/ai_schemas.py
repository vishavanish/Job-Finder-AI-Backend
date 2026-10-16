"""
app/models/ai_schemas.py
--------------------------
Request/response models for the AI model registry, keys, usage and job-source
catalog routes (CLAUDE.md 7.4, 7.9). No response model has a field that can
carry a key value: keys leave the server only as `last4`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, SecretStr

Provider = Literal["gemini", "groq", "huggingface", "anthropic", "openai_compatible"]
Tier = Literal["free", "paid"]
SourceTier = Literal["free", "free_key", "paid", "byok"]
TosRisk = Literal["none", "low", "high"]


# ---------------------------------------------------------------- models

class ModelHealth(BaseModel):
    consecutive_failures: int = 0
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    last_error: Optional[str] = None
    paused: bool = Field(False, description="Circuit breaker open: skipped until the cooldown ends")


class AdminModelOut(BaseModel):
    id: str
    provider: str
    model_id: str
    display_name: str
    tier: str
    tasks: list[str]
    base_url: Optional[str] = None
    key_scope: str
    key_configured: bool = Field(..., description="A platform or .env key exists for key_scope")
    cost_per_1k_in: float
    cost_per_1k_out: float
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    enabled_global: bool
    priority: int
    notes: Optional[str] = None
    health: ModelHealth
    created_at: datetime
    updated_at: datetime


class AdminModelCreate(BaseModel):
    provider: Provider
    model_id: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    tier: Tier = "free"
    tasks: list[str] = Field(default_factory=list, description="Empty = every text task")
    base_url: Optional[str] = Field(None, max_length=512, description="Required for openai_compatible")
    key_scope: Optional[str] = Field(None, max_length=64,
                                     description="Which stored key it uses; defaults to the provider "
                                                 "(or the base_url host for openai_compatible)")
    cost_per_1k_in: float = Field(0.0, ge=0)
    cost_per_1k_out: float = Field(0.0, ge=0)
    context_window: Optional[int] = Field(None, ge=1)
    max_output_tokens: Optional[int] = Field(None, ge=1)
    enabled_global: bool = True
    priority: int = Field(100, ge=0, le=10000)
    notes: Optional[str] = Field(None, max_length=500)


class AdminModelUpdate(BaseModel):
    """Only the fields you send are changed."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    tier: Optional[Tier] = None
    tasks: Optional[list[str]] = None
    base_url: Optional[str] = Field(None, max_length=512)
    key_scope: Optional[str] = Field(None, max_length=64)
    cost_per_1k_in: Optional[float] = Field(None, ge=0)
    cost_per_1k_out: Optional[float] = Field(None, ge=0)
    context_window: Optional[int] = Field(None, ge=1)
    max_output_tokens: Optional[int] = Field(None, ge=1)
    enabled_global: Optional[bool] = None
    priority: Optional[int] = Field(None, ge=0, le=10000)
    notes: Optional[str] = Field(None, max_length=500)


class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: int
    key_source: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    reply: Optional[str] = Field(None, description="First 200 characters of the model's reply")
    error: Optional[str] = None


class TaskDefaults(BaseModel):
    defaults: dict[str, Optional[str]] = Field(..., description="task -> model id (null clears it)")


class UserModelOut(BaseModel):
    id: str
    display_name: str
    provider: str
    model_id: str
    tier: str
    tasks: list[str]
    key_scope: str
    available: bool
    reason: Optional[str] = Field(None, description="Why the model can't be used now")
    uses_own_key: bool
    paused: bool


class ModelCatalogOut(BaseModel):
    tasks: list[str]
    models: list[UserModelOut]
    preferences: dict[str, str]
    defaults: dict[str, str]
    free_only: bool
    premium_models: bool = Field(..., description="Has the premium AI models feature (paid models on platform keys)")


class ModelPreferencesIn(BaseModel):
    preferences: dict[str, Optional[str]] = Field(..., description="task -> model id (null = use the default)")


# ---------------------------------------------------------------- keys

class KeyOut(BaseModel):
    id: str
    scope: str
    owner_type: str
    label: Optional[str] = None
    last4: str
    masked: str
    created_at: datetime
    last_used_at: Optional[datetime] = None


class KeyCreate(BaseModel):
    scope: str = Field(..., min_length=1, max_length=64, description="e.g. gemini, anthropic, openrouter, apify")
    key: SecretStr
    label: Optional[str] = Field(None, max_length=100)


class KeyList(BaseModel):
    keys: list[KeyOut]
    scopes: list[str] = Field(..., description="Key scopes used by enabled models and job sources")


class PlatformKeyScope(BaseModel):
    scope: str
    stored: Optional[KeyOut] = None
    env_fallback: bool = Field(..., description="A key for this scope is set in the server's .env")


class PlatformKeyList(BaseModel):
    keys: list[KeyOut]
    scopes: list[PlatformKeyScope]


# ---------------------------------------------------------------- usage

class UsageTotals(BaseModel):
    calls: int
    failed_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


class UsageByModel(UsageTotals):
    model_ref: Optional[str] = None
    provider: str
    model_id: str


class UsageByDay(UsageTotals):
    day: str


class UsageByUser(UsageTotals):
    user_id: Optional[str] = None
    email: Optional[str] = None


class UsageSummary(BaseModel):
    since: datetime
    totals: UsageTotals
    by_model: list[UsageByModel]
    by_day: list[UsageByDay]
    by_user: Optional[list[UsageByUser]] = None


# ---------------------------------------------------------------- job sources

class SourceHealth(BaseModel):
    consecutive_failures: int = 0
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    last_error: Optional[str] = None
    paused_until: Optional[datetime] = Field(None, description="Skipped after repeated errors until this time")


class UserSourceOut(BaseModel):
    key: str
    name: str
    tier: str
    tos_risk: str
    requires_key: bool
    key_scope: Optional[str] = None
    description: Optional[str] = None
    rate_limit: Optional[str] = None
    enabled: bool = Field(..., description="The user's own switch")
    default_enabled: bool = Field(True, description="Switch position for users who never changed it")
    available: bool = Field(..., description="Will run in the user's next search")
    reason: Optional[str] = None
    health: SourceHealth


class ProviderSettingsOut(BaseModel):
    free_only: bool
    sources: list[UserSourceOut]


class ProviderSettingsIn(BaseModel):
    free_only: Optional[bool] = None
    enabled: dict[str, bool] = Field(default_factory=dict, description="source key -> on/off")


class AdminSourceOut(BaseModel):
    key: str
    name: str
    tier: str
    tos_risk: str
    requires_key: bool
    key_scope: Optional[str] = None
    description: Optional[str] = None
    rate_limit: Optional[str] = None
    enabled_global: bool
    default_enabled: bool = True
    key_configured: Optional[bool] = None
    health: SourceHealth


class AdminSourceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    tier: Optional[SourceTier] = None
    tos_risk: Optional[TosRisk] = None
    rate_limit: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    enabled_global: Optional[bool] = None
    default_enabled: Optional[bool] = Field(None, description="On for users who never switched it themselves")
