"""
app/services/secret_refs.py
------------------------------
Keeps user-supplied API keys (Gemini, Groq, HF, Apify) out of Celery
args. Celery JSON-serializes args into the Redis broker and result
backend, so a raw key passed as an arg sits there in plain text.

Instead, the route calls stash() at the last moment: the key is
Fernet-encrypted into Redis under a random `key_ref` with a TTL, and
only the ref travels through Celery. The worker calls resolve() at run
time. This is the Phase 0 interim; the durable encrypted key store with
BYOK management is Phase 2 (CLAUDE.md 7.9) and should keep the same
ref-in, secret-out contract.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.redis_client import get_redis

KEY_REF_PREFIX = "keyref_"


def master_fernet() -> Fernet:
    """The master key, shared with app/services/key_store.py."""
    return _fernet()


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    if settings.SECRETS_MASTER_KEY:
        return Fernet(settings.SECRETS_MASTER_KEY.encode())
    # Dev-only fallback (production refuses to start without a master
    # key — see config.get_settings). Derived, not random, so the API and
    # worker processes agree on it.
    if not settings.JWT_SECRET_KEY:
        raise RuntimeError("Set SECRETS_MASTER_KEY (or at least JWT_SECRET_KEY in development).")
    digest = hashlib.sha256(b"secret_refs:" + settings.JWT_SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _redis_key(key_ref: str) -> str:
    return f"secret_ref:{key_ref}"


def stash(value: str | None, *, ttl_seconds: int | None = None) -> str:
    """Returns a key_ref for `value`, or "" if there is no value (so the
    worker falls back to the server's own key)."""
    if not value:
        return ""
    key_ref = KEY_REF_PREFIX + secrets.token_urlsafe(24)
    token = _fernet().encrypt(value.encode()).decode()
    ttl = ttl_seconds or get_settings().TASK_RESULT_TTL_SECONDS
    get_redis().set(_redis_key(key_ref), token, ex=ttl)
    return key_ref


def stash_secret_field(value) -> str:
    """stash() for an Optional[SecretStr] request field — unwraps it here,
    so the plain value never exists outside this call."""
    return stash(value.get_secret_value() if value else None)


def resolve(key_ref: str | None, *, fallback: str = "") -> str:
    """Worker-side: the decrypted key for `key_ref`, or `fallback` (the
    server key) when no ref was given. A ref that expired or fails to
    decrypt raises rather than silently using the server's key with a
    request that asked for the user's own."""
    if not key_ref:
        return fallback
    token = get_redis().get(_redis_key(key_ref))
    if token is None:
        raise RuntimeError("API key reference expired or unknown — resubmit the request.")
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("API key reference could not be decrypted (master key changed?).") from e
