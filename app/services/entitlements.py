"""
app/services/entitlements.py
-------------------------------
Which features each user may use (CLAUDE.md 7.11). FEATURES holds the
defaults for everyone; a user_entitlements row overrides them for one
user until its expires_at (after which the default applies again).

require_feature("x") is the reusable route dependency: 403 when the
feature is off for the caller, 429 past the daily quota, 503 when a
global kill switch is off. Managers may only grant a team member what
they hold themselves (manager_may_grant).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import as_utc, get_current_user
from app.core.db import get_db
from app.core.redis_client import get_redis
from app.models.admin import UserEntitlement
from app.services import feature_flags


@dataclass(frozen=True)
class Entitlement:
    feature: str
    enabled: bool
    quota_per_day: int | None = None
    max_level: int | None = None
    expires_at: datetime | None = None
    source: str = "default"  # "default" | "override"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return d


FEATURES: dict[str, Entitlement] = {
    "job_search": Entitlement("job_search", True),
    "scoring": Entitlement("scoring", True),
    "ats_discovery": Entitlement("ats_discovery", True),
    "auto_apply": Entitlement("auto_apply", True, max_level=1),  # level 1 = assist (prefill only)
    "gmail_tracking": Entitlement("gmail_tracking", True),
    "voice_prep": Entitlement("voice_prep", True),
    "live_copilot": Entitlement("live_copilot", False),  # off by default (CLAUDE.md 7.10)
    "resume_tailoring": Entitlement("resume_tailoring", True),
    "premium_models": Entitlement("premium_models", False),
    "job_alerts": Entitlement("job_alerts", True, quota_per_day=5),  # alert runs per day (P3.2)
    # Google Jobs (SerpApi / JSearch / Bright Data) on the platform's own keys. Off: the user needs their own key.
    "shared_search_keys": Entitlement("shared_search_keys", False),
    # P8a: the browser extension saves jobs from pages the user opens (Naukri, Foundit, Glassdoor...).
    "browser_capture": Entitlement("browser_capture", True),
}

FEATURE_LABELS = {
    "job_search": "job search", "scoring": "AI job scoring", "ats_discovery": "company career pages",
    "auto_apply": "auto apply", "gmail_tracking": "Gmail tracking", "voice_prep": "interview practice",
    "live_copilot": "Live Copilot", "resume_tailoring": "resume tailoring", "premium_models": "premium AI models",
    "job_alerts": "job alerts", "shared_search_keys": "shared Google Jobs keys",
    "browser_capture": "saving jobs from your browser",
}

# Global kill switches: feature -> feature flag that must be true.
KILL_SWITCHES = {"auto_apply": "auto_apply_enabled", "live_copilot": "live_copilot_enabled"}


def effective(db: Session, user_id: str, feature: str) -> Entitlement:
    default = FEATURES[feature]
    row = (
        db.query(UserEntitlement)
        .filter(UserEntitlement.user_id == user_id, UserEntitlement.feature == feature)
        .first()
    )
    if row is None:
        return default
    expires = as_utc(row.expires_at)
    if expires is not None and expires <= datetime.now(timezone.utc):
        return default
    return Entitlement(feature, row.enabled, row.quota_per_day, row.max_level, expires, "override")


def all_effective(db: Session, user_id: str) -> list[Entitlement]:
    return [effective(db, user_id, f) for f in FEATURES]


def _quota_key(user_id: str, feature: str) -> str:
    return f"quota:{user_id}:{feature}:{datetime.now(timezone.utc).date().isoformat()}"


def usage_today(user_id: str, feature: str) -> int:
    return int(get_redis().get(_quota_key(user_id, feature)) or 0)


def check_feature(db: Session, user, feature: str) -> Entitlement:
    """Raises if `user` may not use `feature` now; otherwise counts one
    use against today's quota and returns the entitlement."""
    flag = KILL_SWITCHES.get(feature)
    if flag and not feature_flags.get_flag(db, flag):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"{FEATURE_LABELS[feature].capitalize()} is temporarily disabled by the admin.")

    ent = effective(db, user.id, feature)
    if not ent.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Your account does not include {FEATURE_LABELS[feature]}. Ask an admin to enable it.")

    if ent.quota_per_day is not None:
        r = get_redis()
        key = _quota_key(user.id, feature)
        used = r.incr(key)
        if used == 1:
            r.expire(key, 2 * 24 * 3600)
        if used > ent.quota_per_day:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Daily limit for {FEATURE_LABELS[feature]} reached ({ent.quota_per_day}/day).")
    return ent


def require_feature(feature: str):
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}")

    async def _dependency(user=Depends(get_current_user), db: Session = Depends(get_db)):
        check_feature(db, user, feature)
        return user
    return _dependency


def manager_may_grant(db: Session, manager_id: str, feature: str, *, enabled: bool,
                      quota_per_day: int | None, max_level: int | None) -> bool:
    """A manager can't give a team member more than they hold themselves."""
    own = effective(db, manager_id, feature)
    if enabled and not own.enabled:
        return False
    if own.quota_per_day is not None and (quota_per_day is None or quota_per_day > own.quota_per_day):
        return False
    if own.max_level is not None and (max_level is None or max_level > own.max_level):
        return False
    return True


def set_override(db: Session, user_id: str, feature: str, *, enabled: bool, quota_per_day: int | None,
                 max_level: int | None, expires_at: datetime | None) -> tuple[dict, dict]:
    """Upserts the override row. Returns (before, after) for the audit
    entry; the caller audits and commits."""
    before = effective(db, user_id, feature).as_dict()
    row = (
        db.query(UserEntitlement)
        .filter(UserEntitlement.user_id == user_id, UserEntitlement.feature == feature)
        .first()
    )
    if row is None:
        row = UserEntitlement(user_id=user_id, feature=feature)
        db.add(row)
    row.enabled = enabled
    row.quota_per_day = quota_per_day
    row.max_level = max_level
    row.expires_at = expires_at
    after = Entitlement(feature, enabled, quota_per_day, max_level, expires_at, "override").as_dict()
    return before, after


def clear_override(db: Session, user_id: str, feature: str) -> tuple[dict, dict]:
    before = effective(db, user_id, feature).as_dict()
    db.query(UserEntitlement).filter(
        UserEntitlement.user_id == user_id, UserEntitlement.feature == feature
    ).delete()
    return before, FEATURES[feature].as_dict()
