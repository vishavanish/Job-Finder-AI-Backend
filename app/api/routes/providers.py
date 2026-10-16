"""
app/api/routes/providers.py
-----------------------------
The user's job sources and "Free tools only" switch (CLAUDE.md 7.4):

  - GET /providers           every source with tier, ToS risk, health, the
                             user's on/off switch and whether it will run
  - PUT /providers/settings  change Free tools only and per-source switches

Free tools only also hides paid AI models (owner decision, 2026-10-15).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models.ai import UserProviderSetting
from app.models.ai_schemas import ProviderSettingsIn, ProviderSettingsOut
from app.services import audit, provider_catalog

router = APIRouter(prefix="/providers", tags=["providers"])


def _health(row) -> dict:
    return {"consecutive_failures": row.consecutive_failures or 0, "last_success_at": row.last_success_at,
            "last_error_at": row.last_error_at, "last_error": row.last_error,
            "paused_until": provider_catalog.paused_until(row)}


def _view(db: Session, user) -> dict:
    choices = provider_catalog.user_choices(db, user.id)
    sources = []
    for row in provider_catalog.all_sources(db):
        ok, reason = provider_catalog.availability(db, user, row, choices=choices)
        sources.append({
            "key": row.key, "name": row.name, "tier": row.tier, "tos_risk": row.tos_risk,
            "requires_key": row.requires_key, "key_scope": row.key_scope, "description": row.description,
            "rate_limit": row.rate_limit, "enabled": provider_catalog.is_enabled_for(row, choices),
            "default_enabled": bool(row.default_enabled),
            "available": ok, "reason": reason, "health": _health(row),
        })
    return {"free_only": bool(user.free_only_mode), "sources": sources}


@router.get("", response_model=ProviderSettingsOut)
def get_providers(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return _view(db, user)


@router.put("/settings", response_model=ProviderSettingsOut)
def update_settings(req: ProviderSettingsIn, request: Request, user=Depends(get_current_user),
                    db: Session = Depends(get_db)):
    known = {r.key for r in provider_catalog.all_sources(db)}
    unknown = sorted(set(req.enabled) - known)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown source(s): {', '.join(unknown)}")

    before = {"free_only": bool(user.free_only_mode), "enabled": provider_catalog.user_choices(db, user.id)}
    if req.free_only is not None:
        user.free_only_mode = req.free_only
    for key, enabled in req.enabled.items():
        row = (db.query(UserProviderSetting)
               .filter(UserProviderSetting.user_id == user.id, UserProviderSetting.provider_key == key).first())
        if row is None:
            db.add(UserProviderSetting(user_id=user.id, provider_key=key, enabled=enabled))
        else:
            row.enabled = enabled
    db.flush()
    after = {"free_only": bool(user.free_only_mode), "enabled": provider_catalog.user_choices(db, user.id)}
    if after != before:
        audit.record(db, actor_id=user.id, action="providers.user_settings", target_type="user",
                     target_id=user.id, before=before, after=after, request=request)
    db.commit()
    return _view(db, user)

