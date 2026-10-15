"""
app/api/routes/models.py
--------------------------
User side of the AI model registry and key store (CLAUDE.md 7.9):

  - GET  /models              enabled models (Free / Paid), whether each is
                              usable now and why not, the user's choices
  - PUT  /models/preferences  pick a model per task (null = default)
  - GET  /keys, POST /keys, DELETE /keys/{id}   the user's own API keys
                              (BYOK). Values are never returned; last 4 only.
  - GET  /usage               the user's own AI usage and cost
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models.ai_schemas import KeyCreate, KeyList, KeyOut, ModelCatalogOut, ModelPreferencesIn, UsageSummary
from app.services import audit, key_store, model_registry, usage

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelCatalogOut)
def list_models(user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Every model an admin has enabled. `available` / `reason` say whether
    this user can use it right now (paid models need the premium AI models
    feature or the user's own key; Free tools only hides paid ones)."""
    return model_registry.user_catalog(db, user)


@router.put("/models/preferences", response_model=ModelCatalogOut)
def set_preferences(req: ModelPreferencesIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    model_registry.set_user_preferences(db, user, req.preferences)
    db.commit()
    return model_registry.user_catalog(db, user)


@router.get("/keys", response_model=KeyList)
def list_my_keys(user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows = key_store.list_keys(db, owner_type=key_store.OWNER_USER, user_id=user.id)
    return {"keys": [key_store.masked(r) for r in rows], "scopes": model_registry.key_scopes_in_use(db)}


@router.post("/keys", response_model=KeyOut, status_code=201)
def add_my_key(req: KeyCreate, request: Request, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Adds (or replaces) your key for one scope. Models and job sources
    with that scope use it for your requests; it is never shown again."""
    row, before = key_store.save_key(db, owner_type=key_store.OWNER_USER, user_id=user.id, scope=req.scope,
                                     value=req.key.get_secret_value(), label=req.label, actor_id=user.id)
    audit.record(db, actor_id=user.id, action="key.user_set", target_type="key", target_id=row.id,
                 before=before, after=key_store.audit_view(row),
                 request=request)
    db.commit()
    return key_store.masked(row)


@router.delete("/keys/{key_id}", status_code=204)
def delete_my_key(key_id: str, request: Request, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = key_store.get_owned(db, key_id=key_id, owner_type=key_store.OWNER_USER, user_id=user.id)
    audit.record(db, actor_id=user.id, action="key.user_delete", target_type="key", target_id=row.id,
                 before=key_store.audit_view(row), request=request)
    db.delete(row)
    db.commit()


@router.get("/usage", response_model=UsageSummary)
def my_usage(days: int = Query(30, ge=1, le=365), user=Depends(get_current_user), db: Session = Depends(get_db)):
    return usage.summarize(db, user_id=user.id, days=days)
