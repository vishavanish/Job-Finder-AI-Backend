"""
app/api/routes/admin_ai.py
----------------------------
Admin console: AI models, platform keys, job sources and usage
(CLAUDE.md 7.4, 7.9, 7.11). Admins only, except per-user usage, which a
manager may read for their own team. Every change is audited; key values
never appear in responses or audit entries.

  /admin/models                 list, add (any provider, free or paid)
  /admin/models/{id}            edit, delete
  /admin/models/{id}/test       one real call with the platform key
  /admin/models/defaults        default model per task
  /admin/keys                   platform keys (masked) + .env fallbacks
  /admin/providers[/{key}]      job-source catalog: enable, tier, ToS risk
  /admin/usage                  spend and calls by model, day and user
  /admin/users/{id}/usage       one user's usage
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.db import get_db
from app.models.ai import AiModel, ProviderCatalog
from app.models.ai_schemas import (
    AdminModelCreate, AdminModelOut, AdminModelUpdate, AdminSourceOut, AdminSourceUpdate, KeyCreate, KeyOut,
    ModelTestResult, PlatformKeyList, TaskDefaults, UsageSummary,
)
from app.providers.llm import LLMError
from app.services import ai_router, audit, key_store, model_registry, usage, user_admin

router = APIRouter(prefix="/admin", tags=["admin"])
admin_only = require_role("admin")
manager_or_admin = require_role("manager", "admin")


# ============================================================
# AI models
# ============================================================

@router.get("/models", response_model=list[AdminModelOut])
def list_models(actor=Depends(admin_only), db: Session = Depends(get_db)):
    models = db.query(AiModel).order_by(AiModel.priority, AiModel.display_name).all()
    return [model_registry.admin_view(db, m) for m in models]


@router.post("/models", response_model=AdminModelOut, status_code=201)
def add_model(req: AdminModelCreate, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    """Adds a model from any supported provider. Once enabled, every user
    sees it in GET /models (free ones are usable by everyone at once)."""
    model = model_registry.create_model(db, req.model_dump(), actor_id=actor.id)
    audit.record(db, actor_id=actor.id, action="model.create", target_type="ai_model", target_id=model.id,
                 after=model_registry.snapshot(model), request=request)
    db.commit()
    return model_registry.admin_view(db, model)


@router.get("/models/defaults", response_model=TaskDefaults)
def get_defaults(actor=Depends(admin_only), db: Session = Depends(get_db)):
    return {"defaults": model_registry.defaults_map(db)}


@router.put("/models/defaults", response_model=TaskDefaults)
def put_defaults(req: TaskDefaults, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    before = model_registry.defaults_map(db)
    after = model_registry.set_defaults(db, req.defaults, actor_id=actor.id)
    audit.record(db, actor_id=actor.id, action="model.defaults_set", target_type="ai_model_defaults",
                 before=before, after=after, request=request)
    db.commit()
    return {"defaults": after}


@router.patch("/models/{model_ref}", response_model=AdminModelOut)
def edit_model(model_ref: str, req: AdminModelUpdate, request: Request, actor=Depends(admin_only),
               db: Session = Depends(get_db)):
    model = model_registry.get_model(db, model_ref)
    before = model_registry.snapshot(model)
    model_registry.update_model(db, model, req.model_dump(include=req.model_fields_set))
    audit.record(db, actor_id=actor.id, action="model.update", target_type="ai_model", target_id=model.id,
                 before=before, after=model_registry.snapshot(model), request=request)
    db.commit()
    return model_registry.admin_view(db, model)


@router.delete("/models/{model_ref}", status_code=204)
def delete_model(model_ref: str, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    """Removes the model. Users who had picked it go back to the default.
    To hide it temporarily, PATCH enabled_global=false instead."""
    model = model_registry.get_model(db, model_ref)
    audit.record(db, actor_id=actor.id, action="model.delete", target_type="ai_model", target_id=model.id,
                 before=model_registry.snapshot(model), request=request)
    model_registry.delete_model(db, model)
    db.commit()


@router.post("/models/{model_ref}/test", response_model=ModelTestResult)
def test_model(model_ref: str, actor=Depends(admin_only), db: Session = Depends(get_db)):
    """Sends one tiny prompt with the platform (or .env) key. Recorded in the
    usage ledger like any call; a success also clears a tripped breaker."""
    model = model_registry.get_model(db, model_ref)
    resolved = key_store.resolve(db, user_id=None, scope=model.key_scope)
    if resolved is None and model.provider in ai_router.KEY_REQUIRED:
        return ModelTestResult(ok=False, latency_ms=0,
                               error=f"No {model.key_scope} key: add one under Keys or in the server .env.")
    started = time.monotonic()
    try:
        result = ai_router.call_model(
            db, model, user_id=actor.id, task="score", system="You are a connection test.",
            user_prompt='Reply with the single word "OK".', api_key=resolved.value if resolved else "",
            key_source=resolved.source if resolved else "none", max_tokens=200, ref="admin_test",
        )
    except LLMError as e:
        return ModelTestResult(ok=False, latency_ms=int((time.monotonic() - started) * 1000),
                               key_source=resolved.source if resolved else None, error=str(e))
    ai_router.reset_breaker(model.id)
    return ModelTestResult(ok=True, latency_ms=int((time.monotonic() - started) * 1000),
                           key_source=result.key_source, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                           reply=result.text[:200])


# ============================================================
# Platform keys
# ============================================================

@router.get("/keys", response_model=PlatformKeyList)
def list_platform_keys(actor=Depends(admin_only), db: Session = Depends(get_db)):
    rows = key_store.list_keys(db, owner_type=key_store.OWNER_PLATFORM)
    stored = {r.scope: key_store.masked(r) for r in rows}
    scopes = sorted(set(model_registry.key_scopes_in_use(db)) | set(stored) | set(key_store.ENV_FALLBACK))
    from app.core.config import get_settings
    settings = get_settings()
    return {
        "keys": list(stored.values()),
        "scopes": [{"scope": s, "stored": stored.get(s),
                    "env_fallback": bool(key_store.ENV_FALLBACK.get(s)
                                         and getattr(settings, key_store.ENV_FALLBACK[s], ""))}
                   for s in scopes],
    }


@router.post("/keys", response_model=KeyOut, status_code=201)
def set_platform_key(req: KeyCreate, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    """Stores (or replaces) the platform key for a scope. It takes priority
    over the .env key. The value is never shown again."""
    row, before = key_store.save_key(db, owner_type=key_store.OWNER_PLATFORM, user_id=None, scope=req.scope,
                                     value=req.key.get_secret_value(), label=req.label, actor_id=actor.id)
    audit.record(db, actor_id=actor.id, action="key.platform_set", target_type="key", target_id=row.id,
                 before=before, after=key_store.audit_view(row), request=request)
    db.commit()
    return key_store.masked(row)


@router.delete("/keys/{key_id}", status_code=204)
def delete_platform_key(key_id: str, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    row = key_store.get_owned(db, key_id=key_id, owner_type=key_store.OWNER_PLATFORM)
    audit.record(db, actor_id=actor.id, action="key.platform_delete", target_type="key", target_id=row.id,
                 before=key_store.audit_view(row), request=request)
    db.delete(row)
    db.commit()


# ============================================================
# Job-source catalog
# ============================================================

def _source_view(db: Session, row: ProviderCatalog) -> dict:
    return {
        "key": row.key, "name": row.name, "tier": row.tier, "tos_risk": row.tos_risk,
        "requires_key": row.requires_key, "key_scope": row.key_scope, "description": row.description,
        "rate_limit": row.rate_limit, "enabled_global": row.enabled_global,
        "key_configured": key_store.platform_key_available(db, row.key_scope) if row.key_scope else None,
        "health": {"consecutive_failures": row.consecutive_failures or 0, "last_success_at": row.last_success_at,
                   "last_error_at": row.last_error_at, "last_error": row.last_error},
    }


def _source_snapshot(row: ProviderCatalog) -> dict:
    return {"name": row.name, "tier": row.tier, "tos_risk": row.tos_risk, "rate_limit": row.rate_limit,
            "description": row.description, "enabled_global": row.enabled_global}


@router.get("/providers", response_model=list[AdminSourceOut])
def list_sources(actor=Depends(admin_only), db: Session = Depends(get_db)):
    return [_source_view(db, r) for r in db.query(ProviderCatalog).order_by(ProviderCatalog.key).all()]


@router.patch("/providers/{key}", response_model=AdminSourceOut)
def edit_source(key: str, req: AdminSourceUpdate, request: Request, actor=Depends(admin_only),
                db: Session = Depends(get_db)):
    """Turn a source on/off for everyone, or change its tier / ToS risk label.
    New sources need code (a search adapter), so there is no create."""
    row = db.get(ProviderCatalog, key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no job source with this key")
    before = _source_snapshot(row)
    for field, value in req.model_dump(include=req.model_fields_set).items():
        if value is None and field not in ("rate_limit", "description"):
            continue
        setattr(row, field, value)
    audit.record(db, actor_id=actor.id, action="provider.update", target_type="provider", target_id=row.key,
                 before=before, after=_source_snapshot(row), request=request)
    db.commit()
    return _source_view(db, row)


# ============================================================
# Usage
# ============================================================

@router.get("/usage", response_model=UsageSummary)
def all_usage(days: int = Query(30, ge=1, le=365), actor=Depends(admin_only), db: Session = Depends(get_db)):
    return usage.summarize(db, days=days, by_user=True)


@router.get("/users/{user_id}/usage", response_model=UsageSummary)
def user_usage(user_id: str, days: int = Query(30, ge=1, le=365), actor=Depends(manager_or_admin),
               db: Session = Depends(get_db)):
    target = user_admin.get_manageable(db, actor, user_id)
    return usage.summarize(db, user_id=target.id, days=days)
