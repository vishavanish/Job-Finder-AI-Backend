"""
app/providers/google_identity.py
-----------------------------------
Verifies a Google Sign-In ID token (CLAUDE.md 7.1): signature against
Google's published keys, `iss`, `exp`, and `aud` must be one of
GOOGLE_CLIENT_IDS. The caller additionally requires email_verified.

Signing in with Google grants identity only — never mailbox access.
Gmail tracking asks for gmail.readonly separately (Phase 5).
"""
from __future__ import annotations

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.core.config import get_settings


class GoogleTokenInvalid(Exception):
    pass


class GoogleNotConfigured(Exception):
    pass


def verify_id_token(token: str) -> dict:
    audiences = get_settings().google_client_ids_list
    if not audiences:
        raise GoogleNotConfigured("GOOGLE_CLIENT_IDS is not set.")
    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audiences)
    except ValueError as e:  # bad signature, wrong aud/iss, expired, malformed
        raise GoogleTokenInvalid(str(e)) from e
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise GoogleTokenInvalid("unexpected issuer")
    return claims
