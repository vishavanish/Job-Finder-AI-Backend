"""
app/models/admin.py
---------------------
Tables behind the admin console (CLAUDE.md 7.11):

  - UserEntitlement: per-user override of a feature's default (enabled,
    daily quota, max auto-apply level, expiry). No row = the default in
    app/services/entitlements.py applies.
  - AuditLog: append-only record of every admin/manager change and
    security event. Never updated or deleted by the app.
  - FeatureFlag: global switches (invite-only signup, kill switches),
    one JSON value per key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserEntitlement(Base):
    __tablename__ = "user_entitlements"
    __table_args__ = (UniqueConstraint("user_id", "feature", name="uq_entitlement_user_feature"),)

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    feature = Column(String(64), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    quota_per_day = Column(Integer, nullable=True)  # None = unlimited
    max_level = Column(Integer, nullable=True)      # auto_apply autopilot cap (1-3)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class AuditLog(Base):
    """Append-only — never updated or deleted."""
    __tablename__ = "audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_id = Column(String, nullable=True, index=True)  # None = system / anonymous
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(32), nullable=True)
    target_id = Column(String, nullable=True, index=True)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)
    at = Column(DateTime, default=_now, nullable=False, index=True)


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key = Column(String(64), primary_key=True)
    value = Column(JSON, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
