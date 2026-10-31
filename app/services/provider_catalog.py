"""
app/services/provider_catalog.py
-----------------------------------
Job-source catalog and "Free tools only" mode (CLAUDE.md 7.4).

Each source the search code supports is a row in provider_catalog (how to
call it lives in app/providers/job_sources/registry.py). Before a search
is dispatched, effective_sources() drops sources that are:
  - turned off by an admin (enabled_global),
  - turned off by the user (user_provider_settings; a source the user never
    switched follows its `default_enabled`),
  - paid ("paid"/"byok" tier) while the user has Free tools only on,
  - missing a required key (nothing for the user, the platform or .env),
  - paused after SOURCE_BREAKER_FAILURES errors in a row (retried after
    SOURCE_BREAKER_COOLDOWN_SECONDS),
  - remote-only feeds when the search isn't for remote work.
The caller tells the user which were skipped and why. When the request
names no sources, every source that can run is used and the ones that
can't are left out quietly (the Providers page says why).

"naukri_apify" is not a source in the list; SearchRequest.enable_naukri_apify
turns it on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.ai import ProviderCatalog, UserProviderSetting
from app.models.user import User
from app.providers.job_sources import registry
from app.services import key_store

PAID_TIERS = {"paid", "byok"}
TIERS = ("free", "free_key", "paid", "byok")
TOS_RISKS = ("none", "low", "high")
APIFY_SOURCE = "naukri_apify"
REMOTE_ONLY_NOTE = "remote-only feed — add Remote to your work modes or locations"


@dataclass
class SourcePlan:
    sources: list[str] = field(default_factory=list)  # for job_sources.scrape_all
    naukri_apify: bool = False
    skipped: list[str] = field(default_factory=list)  # human-readable reasons


def all_sources(db: Session) -> list[ProviderCatalog]:
    return db.query(ProviderCatalog).order_by(ProviderCatalog.key).all()


def user_choices(db: Session, user_id: str) -> dict[str, bool]:
    rows = db.query(UserProviderSetting).filter(UserProviderSetting.user_id == user_id).all()
    return {r.provider_key: r.enabled for r in rows}


def is_enabled_for(row: ProviderCatalog, choices: dict[str, bool]) -> bool:
    return choices.get(row.key, bool(row.default_enabled))


def user_key_only(source_key: str, *, shared_allowed: bool = False) -> bool:
    """Sources that must run on the user's own key. Outside production a
    platform/.env key is accepted too, for local testing; in production only
    for users an admin gave `shared_search_keys` (`shared_allowed`)."""
    spec = registry.get(source_key)
    return bool(spec and spec.user_key_only) and get_settings().ENVIRONMENT == "production" and not shared_allowed


def may_share_keys(db: Session, user_id: str | None) -> bool:
    """The admin let this user's searches use the platform's Google Jobs keys (entitlement `shared_search_keys`)."""
    from app.services import entitlements
    return bool(user_id) and entitlements.effective(db, user_id, "shared_search_keys").enabled


def shared_keys_allowed(user_id: str | None) -> bool:
    """may_share_keys() with its own session, for search workers that hold none."""
    if not user_id:
        return False
    from app.core.db import SessionLocal
    db = SessionLocal()
    try:
        return may_share_keys(db, user_id)
    finally:
        db.close()


def paused_until(row: ProviderCatalog) -> datetime | None:
    """When a failing source may run again, or None if it isn't paused."""
    settings = get_settings()
    if (row.consecutive_failures or 0) < settings.SOURCE_BREAKER_FAILURES or row.last_error_at is None:
        return None
    last = row.last_error_at if row.last_error_at.tzinfo else row.last_error_at.replace(tzinfo=timezone.utc)
    until = last + timedelta(seconds=settings.SOURCE_BREAKER_COOLDOWN_SECONDS)
    return until if until > datetime.now(timezone.utc) else None


def availability(db: Session, user: User, row: ProviderCatalog, *, choices: dict[str, bool] | None = None,
                 has_request_key: bool = False) -> tuple[bool, str | None]:
    """(runs for this user?, reason when not)."""
    if not row.enabled_global:
        return False, "turned off by the admin"
    if not is_enabled_for(row, choices if choices is not None else user_choices(db, user.id)):
        return False, "turned off in your Providers settings"
    if user.free_only_mode and row.tier in PAID_TIERS:
        return False, "paid source — \"Free tools only\" is on"
    if row.requires_key and row.key_scope and not has_request_key:
        scopes = [s.strip() for s in row.key_scope.split(",") if s.strip()]
        shared_ok = not user_key_only(row.key, shared_allowed=may_share_keys(db, user.id))
        if not any(key_store.has_user_key(db, user.id, s) or (shared_ok and key_store.platform_key_available(db, s))
                   for s in scopes):
            label = " or ".join(_SCOPE_LABELS.get(s, s.title()) for s in scopes)
            article = "an" if label[:1].lower() in "aeiou" else "a"
            return False, (f"needs your own {label} API key" if not shared_ok else f"needs {article} {label} API key")
    if registry.kind(row.key) == "inbox":
        from app.models.email import EmailAccount
        if not db.query(EmailAccount.id).filter(EmailAccount.user_id == user.id,
                                                EmailAccount.status == "active").first():
            return False, "connect Gmail to read your job-alert emails"
    if registry.kind(row.key) == "index":
        from app.services import entitlements
        if not entitlements.effective(db, user.id, "ats_discovery").enabled:
            return False, "your account does not include company career pages"
    until = paused_until(row)
    if until:
        return False, f"paused after repeated errors — retrying after {until:%H:%M} UTC"
    return True, None


_SCOPE_LABELS = {"apify": "Apify", "adzuna": "Adzuna", "jooble": "Jooble", "careerjet": "Careerjet",
                 "serpapi": "SerpApi", "jsearch": "JSearch", "brightdata": "Bright Data", "themuse": "The Muse"}

# Sources a normal search never runs on its own (QA1 §6), with what users see instead of "will run".
NOT_IN_SEARCH = {
    "manual": "you paste these jobs in; searches don't fetch them",
    "apify": "searches from Jobs don't use it yet",
}


def search_availability(db: Session, user: User, row: ProviderCatalog, *,
                        choices: dict[str, bool] | None = None) -> tuple[bool, str | None]:
    """Whether a normal search from Jobs runs this source — what GET /providers
    shows as "will run", so the page and effective_sources() agree."""
    ok, reason = availability(db, user, row, choices=choices)
    note = NOT_IN_SEARCH.get(registry.kind(row.key) or "")
    if ok and note:
        return False, note
    return ok, reason


def effective_sources(db: Session, user: User, *, requested: list[str] | None, want_naukri_apify: bool,
                      has_apify_request_key: bool = False, remote_ok: bool = True) -> SourcePlan:
    """`requested=None` means "every source that can run" (quiet skips)."""
    catalog = {r.key: r for r in all_sources(db)}
    choices = user_choices(db, user.id)
    plan = SourcePlan()
    auto = requested is None
    keys = [k for k in registry.SEARCHABLE_KEYS if k in catalog and k != "naukri"] if auto else requested
    for key in keys:
        row = catalog.get(key)
        if row is None:
            plan.skipped.append(f"{key}: not in the source catalog")
            continue
        ok, reason = availability(db, user, row, choices=choices)
        if ok and registry.kind(key) == "feed" and not remote_ok:
            ok, reason = False, REMOTE_ONLY_NOTE
        if ok:
            plan.sources.append(key)
        elif not auto:
            plan.skipped.append(f"{row.name}: {reason}")
    if want_naukri_apify:
        row = catalog.get(APIFY_SOURCE)
        ok, reason = (availability(db, user, row, choices=choices, has_request_key=has_apify_request_key)
                      if row else (False, "not in the source catalog"))
        if ok:
            plan.naukri_apify = True
        else:
            plan.skipped.append(f"{row.name if row else APIFY_SOURCE}: {reason}")
    return plan


def mark_success(db: Session, keys: set[str]) -> None:
    """Records that these sources answered (health shown to admins)."""
    if not keys:
        return
    now = datetime.now(timezone.utc)
    for row in db.query(ProviderCatalog).filter(ProviderCatalog.key.in_(keys)).all():
        row.last_success_at = now
        row.consecutive_failures = 0
    db.commit()


def mark_failure(db: Session, key: str, error: str) -> None:
    row = db.get(ProviderCatalog, key)
    if row is None:
        return
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_error_at = datetime.now(timezone.utc)
    row.last_error = error[:300]
    db.commit()
