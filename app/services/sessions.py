"""
app/services/sessions.py
---------------------------
Sign-in sessions (CLAUDE.md 7.1): a 15-minute access JWT in the JSON
response plus a long-lived, rotating refresh token in an httpOnly cookie.

Refresh tokens are opaque random strings. Redis keeps only their SHA-256
(`refresh:{hash}` -> user id + issue time) with a TTL, so a Redis dump
can't be replayed. Every /auth/refresh consumes the presented token and
issues a new one. Presenting an already-consumed token means it was
stolen or replayed: all of that user's sessions are revoked.

Revoking all sessions = incrementing users.session_version. Access tokens
(checked in get_current_user) and refresh tokens (checked here) carry the
version they were issued under, so every older one stops working at once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth import account_disabled, create_access_token
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.schemas import TokenResponse, UserOut
from app.models.user import User
from app.services import audit

logger = logging.getLogger("job_finder_api.sessions")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _live_key(token_hash: str) -> str:
    return f"refresh:{token_hash}"


def _used_key(token_hash: str) -> str:
    return f"refresh_used:{token_hash}"


def _ttl_seconds() -> int:
    return get_settings().REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600


def _cookie_path() -> str:
    return f"{get_settings().API_V1_PREFIX}/auth"


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=token,
        max_age=_ttl_seconds(),
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
        path=_cookie_path(),
    )


def clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        path=_cookie_path(),
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
    )


def start_session(user: User, response: Response, db: Session) -> TokenResponse:
    """Issues access + refresh tokens and records the login. Commits."""
    refresh = secrets.token_urlsafe(32)
    get_redis().set(
        _live_key(_hash(refresh)),
        json.dumps({"user_id": user.id, "sv": user.session_version or 0}),
        ex=_ttl_seconds(),
    )
    _set_refresh_cookie(response, refresh)

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return TokenResponse(
        access_token=create_access_token(user.id, session_version=user.session_version or 0),
        user=UserOut.model_validate(user),
    )


def revoke_all_sessions(user: User) -> None:
    """Caller commits (usually together with an audit entry)."""
    user.session_version = (user.session_version or 0) + 1
    user.sessions_revoked_at = datetime.now(timezone.utc)


def rotate(refresh_token: str | None, response: Response, db: Session, request: Request) -> TokenResponse:
    """Consumes `refresh_token` and starts a fresh session, or raises 401."""
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token. Please log in again.")

    token_hash = _hash(refresh_token)
    r = get_redis()
    raw, _ = r.pipeline().get(_live_key(token_hash)).delete(_live_key(token_hash)).execute()

    if raw is None:
        reused_by = r.get(_used_key(token_hash))
        if reused_by:
            user = db.query(User).filter(User.id == reused_by).first()
            if user:
                revoke_all_sessions(user)
                audit.record(db, actor_id=None, action="auth.refresh_token_reuse",
                             target_type="user", target_id=user.id, request=request)
                db.commit()
            logger.warning("refresh token reuse detected for user=%s — all sessions revoked", reused_by)
        clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Please log in again.")

    data = json.loads(raw)
    r.set(_used_key(token_hash), data["user_id"], ex=_ttl_seconds())

    user = db.query(User).filter(User.id == data["user_id"]).first()
    if user is not None and not user.is_active:
        clear_refresh_cookie(response)
        raise account_disabled()
    if user is None or data.get("sv", 0) != (user.session_version or 0):
        clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Please log in again.")

    return start_session(user, response, db)


def end_session(refresh_token: str | None, response: Response) -> None:
    if refresh_token:
        get_redis().delete(_live_key(_hash(refresh_token)))
    clear_refresh_cookie(response)
