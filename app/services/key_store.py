"""
app/services/key_store.py
----------------------------
Durable encrypted store for API keys (CLAUDE.md 7.9, 12): platform keys an
admin saves and BYOK keys users add for themselves.

Envelope encryption: every secret gets its own random data key; the value
is encrypted with that data key, and the data key is encrypted ("wrapped")
with the master key (SECRETS_MASTER_KEY, same one secret_refs uses). Rotating
the master key only means re-wrapping the small data keys.

Rules:
  - A value is never returned by any API; listings show `last4` only.
  - Workers look keys up here at run time by (user_id, scope) — no key or
    ref travels through Celery for stored keys.
  - Lookup order for a scope: the user's own key -> the platform key -> the
    server's .env key (ENV_FALLBACK), so existing deployments keep working.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.ai import Secret
from app.services.secret_refs import master_fernet

OWNER_PLATFORM = "platform"
OWNER_USER = "user"

# scope -> Settings attribute used when nothing is stored.
ENV_FALLBACK = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "huggingface": "HF_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "apify": "APIFY_API_TOKEN",
    "adzuna": "ADZUNA_API_KEY",
    "jooble": "JOOBLE_API_KEY",
    "careerjet": "CAREERJET_API_KEY",
}

MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 1000


@dataclass(frozen=True)
class ResolvedKey:
    value: str
    source: str  # byok | platform | env


def normalize_scope(scope: str) -> str:
    s = (scope or "").strip().lower()
    if not s or len(s) > 64 or not all(c.isalnum() or c in "-_." for c in s):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Key scope must be 1-64 letters, digits, '-', '_' or '.'.")
    return s


def _encrypt(value: str) -> tuple[str, str]:
    data_key = Fernet.generate_key()
    wrapped = master_fernet().encrypt(data_key).decode()
    ciphertext = Fernet(data_key).encrypt(value.encode()).decode()
    return wrapped, ciphertext


def _decrypt(row: Secret) -> str:
    try:
        data_key = master_fernet().decrypt(row.wrapped_key.encode())
        return Fernet(data_key).decrypt(row.ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(f"stored key {row.id} could not be decrypted (master key changed?)") from e


def masked(row: Secret) -> dict:
    """The only shape a stored key ever leaves this module in."""
    return {
        "id": row.id,
        "scope": row.scope,
        "owner_type": row.owner_type,
        "label": row.label,
        "last4": row.last4,
        "masked": f"••••{row.last4}",
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
    }


def audit_view(row: Secret) -> dict:
    """JSON-safe description for the audit log (never the value)."""
    return {"scope": row.scope, "owner_type": row.owner_type, "label": row.label, "last4": row.last4}


def _find(db: Session, owner_type: str, user_id: str | None, scope: str) -> Secret | None:
    q = db.query(Secret).filter(Secret.owner_type == owner_type, Secret.scope == scope)
    q = q.filter(Secret.user_id == user_id) if user_id else q.filter(Secret.user_id.is_(None))
    return q.first()


def save_key(db: Session, *, owner_type: str, user_id: str | None, scope: str, value: str,
             label: str | None = None, actor_id: str | None = None) -> tuple[Secret, dict | None]:
    """Adds or replaces the key for (owner, scope). Returns (row, masked
    previous row or None, via audit_view) so the caller can audit the change. Caller commits."""
    value = (value or "").strip()
    if not (MIN_KEY_LENGTH <= len(value) <= MAX_KEY_LENGTH) or any(c.isspace() for c in value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "That doesn't look like an API key (8-1000 characters, no spaces).")
    scope = normalize_scope(scope)
    if owner_type == OWNER_USER and not user_id:
        raise ValueError("user keys need a user_id")

    existing = _find(db, owner_type, user_id if owner_type == OWNER_USER else None, scope)
    before = audit_view(existing) if existing else None
    wrapped, ciphertext = _encrypt(value)
    row = existing or Secret(owner_type=owner_type, user_id=user_id if owner_type == OWNER_USER else None,
                             scope=scope)
    row.wrapped_key = wrapped
    row.ciphertext = ciphertext
    row.last4 = value[-4:]
    row.label = (label or "").strip()[:100] or None
    row.created_by = actor_id
    if existing is None:
        db.add(row)
    db.flush()
    return row, before


def list_keys(db: Session, *, owner_type: str, user_id: str | None = None) -> list[Secret]:
    q = db.query(Secret).filter(Secret.owner_type == owner_type)
    if owner_type == OWNER_USER:
        q = q.filter(Secret.user_id == user_id)
    return q.order_by(Secret.scope).all()


def get_owned(db: Session, *, key_id: str, owner_type: str, user_id: str | None = None) -> Secret:
    """404 unless the key belongs to this owner (users never see others' keys)."""
    row = db.get(Secret, key_id)
    if row is None or row.owner_type != owner_type or (owner_type == OWNER_USER and row.user_id != user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no key found with this id")
    return row


def has_user_key(db: Session, user_id: str | None, scope: str) -> bool:
    return bool(user_id) and _find(db, OWNER_USER, user_id, scope) is not None


def user_key_scopes(db: Session, user_id: str | None) -> set[str]:
    if not user_id:
        return set()
    rows = db.query(Secret.scope).filter(Secret.owner_type == OWNER_USER, Secret.user_id == user_id).all()
    return {r[0] for r in rows}


def resolve(db: Session, *, user_id: str | None, scope: str) -> ResolvedKey | None:
    """Worker-side lookup: the user's own key, else the platform key, else
    the .env key. None when nothing is configured for this scope."""
    if user_id:
        row = _find(db, OWNER_USER, user_id, scope)
        if row:
            return ResolvedKey(_decrypt(row), "byok")
    row = _find(db, OWNER_PLATFORM, None, scope)
    if row:
        return ResolvedKey(_decrypt(row), "platform")
    attr = ENV_FALLBACK.get(scope)
    value = getattr(get_settings(), attr, "") if attr else ""
    return ResolvedKey(value, "env") if value else None


def platform_key_available(db: Session, scope: str) -> bool:
    if _find(db, OWNER_PLATFORM, None, scope):
        return True
    attr = ENV_FALLBACK.get(scope)
    return bool(attr and getattr(get_settings(), attr, ""))
