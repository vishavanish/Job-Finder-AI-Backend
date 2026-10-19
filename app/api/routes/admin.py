"""
app/api/routes/admin.py
--------------------------
Admin console API (CLAUDE.md 7.11). Thin: authorize, validate, call
app/services/user_admin.py / entitlements.py / feature_flags.py.

Access: admins everything; managers only list/view their team, enable or
disable them, reset their sessions and change their entitlements within
the manager's own limits. Every change is written to the audit log.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.db import get_db
from app.models.admin import AuditLog
from app.models.schemas import (
    AdminUserCreate, AdminUserList, AdminUserUpdate, AuditEntryOut, AuditList, EntitlementOut,
    EntitlementUpdate, FlagUpdate, UserOut,
)
from app.services import audit, entitlements, feature_flags, user_admin

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("job_finder_api.admin")

admin_only = require_role("admin")
manager_or_admin = require_role("manager", "admin")


# ============================================================
# Users
# ============================================================

@router.get("/users", response_model=AdminUserList)
def list_users(
    q: Optional[str] = Query(None, description="Substring of email or name"),
    role: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    actor=Depends(manager_or_admin),
    db: Session = Depends(get_db),
):
    """Admins see every user; managers see only their team."""
    rows, total = user_admin.search_users(db, actor, q=q, role=role, active=active, limit=limit, offset=offset)
    return AdminUserList(users=[UserOut.model_validate(u) for u in rows], total=total, limit=limit, offset=offset)


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(req: AdminUserCreate, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    """Creates (invites) an account. The person signs in with an emailed
    code or Google using this address — works in invite-only mode."""
    user = user_admin.create_user(db, actor, email=req.email, name=req.name, role=req.role,
                                  manager_id=req.manager_id, request=request)
    return UserOut.model_validate(user)


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: str, actor=Depends(manager_or_admin), db: Session = Depends(get_db)):
    return UserOut.model_validate(user_admin.get_manageable(db, actor, user_id))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, req: AdminUserUpdate, request: Request, actor=Depends(admin_only),
                db: Session = Depends(get_db)):
    target = user_admin.get_manageable(db, actor, user_id)
    changes = req.model_dump(include=req.model_fields_set)
    if "role" in changes and changes["role"] is None:
        changes.pop("role")
    return UserOut.model_validate(user_admin.update_user(db, actor, target, changes, request))


@router.post("/users/{user_id}/disable", response_model=UserOut)
def disable_user(user_id: str, request: Request, actor=Depends(manager_or_admin), db: Session = Depends(get_db)):
    """Locks the user out at once: sessions revoked, running tasks and
    streaming runs cancelled."""
    target = user_admin.get_manageable(db, actor, user_id)
    return UserOut.model_validate(user_admin.set_active(db, actor, target, False, request))


@router.post("/users/{user_id}/enable", response_model=UserOut)
def enable_user(user_id: str, request: Request, actor=Depends(manager_or_admin), db: Session = Depends(get_db)):
    target = user_admin.get_manageable(db, actor, user_id)
    return UserOut.model_validate(user_admin.set_active(db, actor, target, True, request))


@router.post("/users/{user_id}/revoke-sessions", status_code=204)
def revoke_sessions(user_id: str, request: Request, actor=Depends(manager_or_admin), db: Session = Depends(get_db)):
    """Signs the user out everywhere (they can sign in again)."""
    user_admin.reset_sessions(db, actor, user_admin.get_manageable(db, actor, user_id), request)


# ============================================================
# Entitlements
# ============================================================

def _feature_or_404(feature: str) -> str:
    if feature not in entitlements.FEATURES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown feature {feature!r}")
    return feature


@router.get("/features", response_model=list[EntitlementOut])
def list_features(actor=Depends(manager_or_admin)):
    """Every feature with its default for users who have no override."""
    return [EntitlementOut(**e.as_dict()) for e in entitlements.FEATURES.values()]


@router.get("/users/{user_id}/entitlements", response_model=list[EntitlementOut])
def get_entitlements(user_id: str, actor=Depends(manager_or_admin), db: Session = Depends(get_db)):
    target = user_admin.get_manageable(db, actor, user_id)
    return [
        EntitlementOut(**e.as_dict(), used_today=entitlements.usage_today(target.id, e.feature))
        for e in entitlements.all_effective(db, target.id)
    ]


@router.put("/users/{user_id}/entitlements/{feature}", response_model=EntitlementOut)
def set_entitlement(user_id: str, feature: str, req: EntitlementUpdate, request: Request,
                    actor=Depends(manager_or_admin), db: Session = Depends(get_db)):
    feature = _feature_or_404(feature)
    target = user_admin.get_manageable(db, actor, user_id)
    if actor.role != "admin" and not entitlements.manager_may_grant(
        db, actor.id, feature, enabled=req.enabled, quota_per_day=req.quota_per_day, max_level=req.max_level,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Managers can't grant more than their own limits.")

    before, after = entitlements.set_override(
        db, target.id, feature, enabled=req.enabled, quota_per_day=req.quota_per_day,
        max_level=req.max_level, expires_at=req.expires_at,
    )
    audit.record(db, actor_id=actor.id, action="entitlement.set", target_type="user", target_id=target.id,
                 before=before, after=after, request=request)
    db.commit()
    return EntitlementOut(**entitlements.effective(db, target.id, feature).as_dict())


@router.delete("/users/{user_id}/entitlements/{feature}", response_model=EntitlementOut)
def reset_entitlement(user_id: str, feature: str, request: Request, actor=Depends(manager_or_admin),
                      db: Session = Depends(get_db)):
    """Removes the override, so the feature default applies again."""
    feature = _feature_or_404(feature)
    target = user_admin.get_manageable(db, actor, user_id)
    default = entitlements.FEATURES[feature]
    if actor.role != "admin" and not entitlements.manager_may_grant(
        db, actor.id, feature, enabled=default.enabled, quota_per_day=default.quota_per_day,
        max_level=default.max_level,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Managers can't grant more than their own limits.")

    before, after = entitlements.clear_override(db, target.id, feature)
    audit.record(db, actor_id=actor.id, action="entitlement.reset", target_type="user", target_id=target.id,
                 before=before, after=after, request=request)
    db.commit()
    return EntitlementOut(**entitlements.effective(db, target.id, feature).as_dict())


# ============================================================
# Feature flags & kill switches
# ============================================================

@router.get("/flags")
def get_flags(actor=Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    return feature_flags.all_flags(db)


@router.put("/flags/{key}")
def set_flag(key: str, req: FlagUpdate, request: Request, actor=Depends(admin_only),
             db: Session = Depends(get_db)) -> dict:
    if key not in feature_flags.FLAGS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown flag {key!r}")
    expected = type(feature_flags.FLAGS[key]())
    if not isinstance(req.value, expected):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key} must be a {expected.__name__}")
    before, after = feature_flags.set_flag(db, key, req.value, actor_id=actor.id)
    audit.record(db, actor_id=actor.id, action="flag.set", target_type="flag", target_id=key,
                 before={"value": before}, after={"value": after}, request=request)
    db.commit()
    if key == "auto_apply_enabled" and after is False:  # global kill switch: stop every active apply run
        from app.models.apply import RUN_ACTIVE_STATES, ApplyRun
        from app.services import apply_engine
        for (user_id,) in db.query(ApplyRun.user_id).filter(ApplyRun.state.in_(RUN_ACTIVE_STATES)).distinct():
            apply_engine.cancel_active_runs(db, user_id, reason="Auto apply was turned off by the admin.")
    return feature_flags.all_flags(db)


# ============================================================
# Audit log
# ============================================================

@router.get("/audit", response_model=AuditList)
def list_audit(
    actor_id: Optional[str] = Query(None),
    target_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    actor=Depends(admin_only),
    db: Session = Depends(get_db),
):
    query = db.query(AuditLog)
    if actor_id:
        query = query.filter(AuditLog.actor_id == actor_id)
    if target_id:
        query = query.filter(AuditLog.target_id == target_id)
    if action:
        query = query.filter(AuditLog.action == action)
    total = query.count()
    rows = query.order_by(AuditLog.at.desc()).offset(offset).limit(limit).all()
    return AuditList(entries=[AuditEntryOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)
