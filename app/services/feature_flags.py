"""
app/services/feature_flags.py
--------------------------------
Global switches an admin flips at runtime (CLAUDE.md 7.11). Only keys
listed in FLAGS exist; a flag with no row uses its default.

  - invite_only: when true, unknown emails cannot sign up (OTP, Google or
    password); admins create accounts instead. Defaults to the inverse of
    the ALLOW_SELF_REGISTRATION env setting from Phase 0.
  - auto_apply_enabled: global kill switch for /apply (and the apply step
    of /pipeline/run), checked by require_feature("auto_apply").
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.admin import FeatureFlag

FLAGS: dict[str, Callable[[], Any]] = {
    "invite_only": lambda: not get_settings().ALLOW_SELF_REGISTRATION,
    "auto_apply_enabled": lambda: True,
}


def get_flag(db: Session, key: str) -> Any:
    row = db.get(FeatureFlag, key)
    return row.value if row is not None else FLAGS[key]()


def all_flags(db: Session) -> dict[str, Any]:
    return {key: get_flag(db, key) for key in FLAGS}


def set_flag(db: Session, key: str, value: Any, *, actor_id: str) -> tuple[Any, Any]:
    """Returns (before, after). Caller audits and commits."""
    if key not in FLAGS:
        raise KeyError(key)
    before = get_flag(db, key)
    row = db.get(FeatureFlag, key)
    if row is None:
        row = FeatureFlag(key=key)
        db.add(row)
    row.value = value
    row.updated_by = actor_id
    return before, value
