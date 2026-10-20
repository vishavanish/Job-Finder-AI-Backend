"""
app/services/voice_auth.py
----------------------------
Socket auth for voice sessions (P7 plan 2.6). Browsers can't set headers on
a WebSocket and tokens must not travel in URLs, so:

  1. POST /voice/sessions (normal bearer auth) returns a stream token: a JWT
     (typ "voice_stream", VOICE_STREAM_TOKEN_SECONDS) bound to the user and
     the session, with a jti stored in Redis.
  2. The client's FIRST socket message is {"type": "auth", "token": ...}.
     consume() checks it and deletes the jti, so a token works once.

One live voice session per user: a Redis lock holds the session id while a
socket is open (refreshed while it talks; expires if the process dies).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.auth import JWT_ALGORITHM
from app.core.config import get_settings
from app.core.redis_client import get_redis

LIVE_LOCK_SECONDS = 90


def _jti_key(jti: str) -> str:
    return f"voice_tok:{jti}"


def _live_key(user_id: str) -> str:
    return f"voice_live:{user_id}"


def issue(user_id: str, session_id: str, session_version: int) -> str:
    s = get_settings()
    jti = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    get_redis().set(_jti_key(jti), session_id, ex=s.VOICE_STREAM_TOKEN_SECONDS)
    return jwt.encode({"sub": user_id, "sid": session_id, "sv": session_version, "jti": jti, "typ": "voice_stream",
                       "iat": int(now.timestamp()),
                       "exp": now + timedelta(seconds=s.VOICE_STREAM_TOKEN_SECONDS)},
                      s.JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def consume(token: str, session_id: str) -> dict | None:
    """The claims when the token is valid, unused, and for this session; else None."""
    try:
        claims = jwt.decode(token or "", get_settings().JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    if claims.get("typ") != "voice_stream" or claims.get("sid") != session_id or not claims.get("jti"):
        return None
    if not get_redis().delete(_jti_key(claims["jti"])):
        return None  # already used (or expired)
    return claims


def acquire_live(user_id: str, session_id: str) -> bool:
    r = get_redis()
    if r.set(_live_key(user_id), session_id, nx=True, ex=LIVE_LOCK_SECONDS):
        return True
    if r.get(_live_key(user_id)) == session_id:  # the same session reconnecting
        r.expire(_live_key(user_id), LIVE_LOCK_SECONDS)
        return True
    return False


def refresh_live(user_id: str, session_id: str) -> None:
    r = get_redis()
    if r.get(_live_key(user_id)) == session_id:
        r.expire(_live_key(user_id), LIVE_LOCK_SECONDS)


def release_live(user_id: str, session_id: str) -> None:
    r = get_redis()
    if r.get(_live_key(user_id)) == session_id:
        r.delete(_live_key(user_id))


def live_session(user_id: str) -> str | None:
    return get_redis().get(_live_key(user_id))
