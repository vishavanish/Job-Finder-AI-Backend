"""
app/core/auth.py
------------------
All authentication logic in one place: password hashing, JWT
issuing/verification, and the two FastAPI dependencies that use them.

Two auth mechanisms live here side by side during the migration to real
user accounts:

1. require_api_key — the original minimal API-key check ("is the caller
   allowed to use this deployment at all"). Kept for any route not yet
   migrated to per-user accounts, and as a fallback for machine-to-machine
   callers that don't have a user login.

2. get_current_user — JWT-based, backed by the users table (SQLite via
   app/core/db.py). Use this on routes that need to know WHICH user is
   calling (e.g. so /applications can scope results per-user later).

If API_KEYS is empty, require_api_key auth is OPEN — every request
passes. This matches local-dev behaviour by default, but get_settings()
already logs a loud startup warning when this is the case (see
config.py), and this module logs on every unauthenticated request too,
so it's hard to miss in production logs if you forgot to set keys.

get_current_user has NO equivalent open mode — a missing/invalid/expired
JWT is always a 401 regardless of server config. If a route should be
public, don't attach this dependency at all.
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


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ============================================================
# JWT issuing / verification
# ============================================================

def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """Returns the user id (the token's `sub` claim) if the token is
    valid and unexpired, else None. Never raises — callers check for
    None and respond 401."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# ============================================================
# FastAPI dependencies
# ============================================================

async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """FastAPI dependency — add to routes/routers that need protecting:

        router = APIRouter(prefix="/pipeline", dependencies=[Depends(require_api_key)])

    or per-route:

        @router.post("/run", dependencies=[Depends(require_api_key)])
    """
    settings = get_settings()

    if not settings.api_keys_list:
        # No keys configured server-side -> auth is open. Logged (not just
        # silently allowed) so it shows up if this ever happens live.
        logger.warning("request allowed with NO auth check — API_KEYS is empty in settings")
        return "unauthenticated"

    if not x_api_key or x_api_key not in settings.api_keys_list:
        logger.warning("rejected request: missing or invalid X-API-Key header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Send a valid key in the X-API-Key header.",
        )

    return x_api_key


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """FastAPI dependency for routes that need to know WHICH user is
    calling, not just "is this caller allowed." Expects a JWT issued by
    POST /auth/login or /auth/register, sent as:

        Authorization: Bearer <token>
    """
    # Imported here rather than at module top to avoid a circular import:
    # app.models.user imports Base from app.core.db, and this module also
    # imports from app.core.db — deferring the User import to call-time
    # sidesteps any load-order ambiguity at negligible per-request cost.
    from app.models.user import User

    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("rejected request: missing or malformed Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Send 'Bearer <token>'.",
        )

    token = authorization.removeprefix("Bearer ").strip()
    user_id = decode_access_token(token)
    if not user_id:
        logger.warning("rejected request: invalid or expired JWT")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please log in again.",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        logger.warning("rejected request: token valid but user %s not found or inactive", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive.",
        )

    return user