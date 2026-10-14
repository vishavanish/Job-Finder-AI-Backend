"""
app/core/auth.py
------------------
Password hashing, access-token issuing/verification, and the FastAPI
dependencies that authenticate and authorize every request.

Access tokens are short-lived HS256 JWTs (ACCESS_TOKEN_EXPIRE_MINUTES,
default 15) carrying only the user id and issue time. Everything that
decides access — is_active, role, session_version — is read from the
database on every request, so disabling a user, changing a role or
resetting sessions takes effect on the very next call (CLAUDE.md 7.1).
Long-lived refresh tokens live in app/services/sessions.py.

require_api_key is the original pre-accounts mechanism; no route uses it
any more, it is kept only for machine-to-machine callers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException, status, Depends
from sqlalchemy.orm import Session
from jose import jwt, JWTError
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.db import get_db

logger = logging.getLogger("job_finder_api.auth")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
JWT_ALGORITHM = "HS256"


# ============================================================
# Password hashing
# ============================================================

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False  # OTP/Google-only account: no password to match
    return pwd_context.verify(plain, hashed)


# ============================================================
# Time helpers (DB columns are naive UTC on both SQLite and Postgres)
# ============================================================

def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ============================================================
# Access tokens
# ============================================================

def create_access_token(subject: str, expires_minutes: int | None = None, *, session_version: int = 0) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": subject,
        "exp": expire,
        "iat": int(now.timestamp()),
        "sv": session_version,  # must equal users.session_version (see app/models/user.py)
        "typ": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """The token's claims if valid and unexpired, else None. Never raises."""
    settings = get_settings()
    try:
        claims = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    if claims.get("typ", "access") != "access" or not claims.get("sub"):
        return None
    return claims


# ============================================================
# FastAPI dependencies
# ============================================================

async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    settings = get_settings()

    if not settings.api_keys_list:
        logger.warning("request allowed with NO auth check — API_KEYS is empty in settings")
        return "unauthenticated"

    if not x_api_key or x_api_key not in settings.api_keys_list:
        logger.warning("rejected request: missing or invalid X-API-Key header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Send a valid key in the X-API-Key header.",
        )

    return x_api_key


def account_disabled() -> HTTPException:
    """403 with a machine-readable code so the frontend can sign the user
    out with a clear message (frontend spec 5.2)."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "account_disabled", "message": "This account has been disabled. Contact your admin."},
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Authenticates `Authorization: Bearer <access token>` and loads the
    user. 401 if the token is missing/invalid/expired, the user is gone
    or disabled, or the token's session version was revoked."""
    # Imported at call time to avoid a circular import (models -> db -> ...).
    from app.models.user import User

    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("rejected request: missing or malformed Authorization header")
        raise _unauthorized("Missing or invalid Authorization header. Send 'Bearer <token>'.")

    claims = decode_access_token(authorization.removeprefix("Bearer ").strip())
    if not claims:
        logger.warning("rejected request: invalid or expired JWT")
        raise _unauthorized("Invalid or expired token. Please log in again.")

    user = db.query(User).filter(User.id == claims["sub"]).first()
    if not user:
        logger.warning("rejected request: token valid but user %s not found", claims["sub"])
        raise _unauthorized("User not found.")
    if not user.is_active:
        logger.warning("rejected request: user %s is disabled", user.id)
        raise account_disabled()

    if claims.get("sv", 0) != (user.session_version or 0):
        logger.warning("rejected request: token for user %s issued before session revocation", user.id)
        raise _unauthorized("Session has been revoked. Please log in again.")

    return user


def require_role(*roles: str):
    """Dependency factory: `Depends(require_role("admin"))` or
    `Depends(require_role("manager", "admin"))`. Enforced server-side on
    every admin route, never only in the UI."""
    async def _check(user=Depends(get_current_user)):
        if user.role not in roles:
            logger.warning("forbidden: user=%s role=%s needs one of %s", user.id, user.role, roles)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to do this.")
        return user
    return _check
