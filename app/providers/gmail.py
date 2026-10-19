"""
app/providers/gmail.py
-------------------------
Gmail access for P5, behind one small class so the sync can be tested with
a fake (CLAUDE.md 7.8).

OAuth (web flow, per user): build_auth_url -> Google consent ->
/gmail/callback -> exchange_code. Scopes: gmail.readonly + openid email.
Signing in with Google never grants mailbox access; this is a separate,
explicit consent. Tokens are sealed by the caller (key_store.seal).

GmailClient(refresh_token): profile, message ids (search or history),
message metadata, message bodies (text + html), all read-only.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [READONLY, "openid", "email"]


class GmailNotConfigured(Exception):
    pass


class GrantRevoked(Exception):
    """The user revoked access or the refresh token expired: reconnect needed."""


class HistoryExpired(Exception):
    """startHistoryId is too old for history.list; fall back to a search."""


class GmailError(Exception):
    pass


# ---------------------------------------------------------------- OAuth

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(state: str, code_challenge: str, login_hint: str | None = None) -> str:
    s = get_settings()
    if not (s.GMAIL_CLIENT_ID and s.GMAIL_CLIENT_SECRET):
        raise GmailNotConfigured("Gmail connection isn't set up on this server (GMAIL_CLIENT_ID / SECRET).")
    params = {
        "client_id": s.GMAIL_CLIENT_ID, "redirect_uri": s.GMAIL_REDIRECT_URI, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URI}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str) -> dict:
    """{access_token, refresh_token, scope, ...}. Raises GmailError."""
    s = get_settings()
    try:
        r = httpx.post(TOKEN_URI, data={
            "code": code, "client_id": s.GMAIL_CLIENT_ID, "client_secret": s.GMAIL_CLIENT_SECRET,
            "redirect_uri": s.GMAIL_REDIRECT_URI, "grant_type": "authorization_code", "code_verifier": code_verifier,
        }, timeout=20)
    except httpx.HTTPError as e:
        raise GmailError(f"token exchange failed: {type(e).__name__}") from e
    if r.status_code != 200:
        raise GmailError(f"token exchange failed: HTTP {r.status_code}")
    return r.json()


def revoke(token: str) -> None:
    """Best effort: a failure here never blocks disconnecting."""
    try:
        httpx.post(REVOKE_URI, data={"token": token}, timeout=10)
    except httpx.HTTPError:
        pass


# ---------------------------------------------------------------- messages

@dataclass
class MessageMeta:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime


def received_at(msg: dict) -> datetime:
    """When Gmail received the message (internalDate, epoch ms)."""
    internal_ms = msg.get("internalDate")
    if internal_ms:
        return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8") + b"===").decode("utf-8", errors="replace")


def bodies(payload: dict) -> tuple[str, str]:
    """(plain text, html) from a Gmail message payload, walking multipart parts."""
    text, html = "", ""

    def walk(part: dict) -> None:
        nonlocal text, html
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain" and not text:
            text = _decode(data)
        elif data and mime == "text/html" and not html:
            html = _decode(data)
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    if not text and html:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    return text, html


def meta_from(msg: dict) -> MessageMeta:
    headers = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}
    return MessageMeta(message_id=msg["id"], thread_id=msg.get("threadId", ""), sender=headers.get("from", ""),
                       subject=headers.get("subject", ""), snippet=msg.get("snippet", ""),
                       received_at=received_at(msg))


class GmailClient:
    """Read-only Gmail API calls for one account."""

    def __init__(self, refresh_token: str):
        s = get_settings()
        if not (s.GMAIL_CLIENT_ID and s.GMAIL_CLIENT_SECRET):
            raise GmailNotConfigured("Gmail connection isn't set up on this server.")
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(token=None, refresh_token=refresh_token, token_uri=TOKEN_URI,
                            client_id=s.GMAIL_CLIENT_ID, client_secret=s.GMAIL_CLIENT_SECRET, scopes=[READONLY])
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _call(self, request):
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        try:
            return request.execute()
        except RefreshError as e:
            raise GrantRevoked(str(e)[:200]) from e
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status == 404:
                raise HistoryExpired() from e
            if status in (401, 403):
                raise GrantRevoked(f"HTTP {status}") from e
            raise GmailError(f"HTTP {status}") from e

    def profile(self) -> dict:
        return self._call(self._svc.users().getProfile(userId="me"))

    def search(self, query: str, max_results: int) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < max_results:
            resp = self._call(self._svc.users().messages().list(
                userId="me", q=query, maxResults=min(100, max_results - len(ids)), pageToken=token))
            ids += [m["id"] for m in resp.get("messages", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids

    def history(self, start_history_id: str, max_results: int) -> tuple[list[str], str | None]:
        """(new message ids, latest history id). Raises HistoryExpired."""
        ids: list[str] = []
        token = None
        latest = None
        while len(ids) < max_results:
            resp = self._call(self._svc.users().history().list(
                userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"], pageToken=token))
            latest = resp.get("historyId", latest)
            for h in resp.get("history", []):
                ids += [m["message"]["id"] for m in h.get("messagesAdded", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return list(dict.fromkeys(ids))[:max_results], latest

    def metadata(self, message_id: str) -> MessageMeta:
        msg = self._call(self._svc.users().messages().get(
            userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"]))
        return meta_from(msg)

    def body(self, message_id: str) -> tuple[str, str]:
        msg = self._call(self._svc.users().messages().get(userId="me", id=message_id, format="full"))
        return bodies(msg.get("payload") or {})
