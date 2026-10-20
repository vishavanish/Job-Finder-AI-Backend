"""
app/services/gmail_accounts.py
---------------------------------
Connecting and disconnecting Gmail accounts (P5 plan §2.1).

start_connect: a single-use `state` in Redis (10 min) holds the user id and
the PKCE verifier; the browser goes to Google's consent screen.
finish_connect: the callback (no bearer token — it's a browser redirect)
trusts only that state. It requires the gmail.readonly scope, reads the
address, and stores the refresh token sealed (key_store.seal).
disconnect: revokes at Google (best effort) and deletes the account with its
events and alert jobs; applications keep their statuses and history.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.email import EmailAccount, EmailEvent, EmailJob
from app.providers import gmail
from app.services import key_store

logger = logging.getLogger("job_finder_api.gmail_accounts")
STATE_TTL_SECONDS = 600


def _error(code: int, err: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": err, "message": message})


def _state_key(state: str) -> str:
    return f"gmail_oauth_state:{state}"


def client_for(account: EmailAccount):
    """The Gmail client for an account (tests replace this)."""
    return gmail.GmailClient(key_store.unseal(account.token_sealed))


def start_connect(db: Session, user_id: str, login_hint: str | None = None) -> str:
    limit = get_settings().GMAIL_ACCOUNTS_MAX_PER_USER
    count = db.query(func.count(EmailAccount.id)).filter(EmailAccount.user_id == user_id).scalar() or 0
    if count >= limit:
        raise _error(409, "gmail_limit", f"You can connect up to {limit} Gmail accounts. Disconnect one first.")
    verifier, challenge = gmail.pkce_pair()
    state = secrets.token_urlsafe(32)
    get_redis().set(_state_key(state), json.dumps({"user_id": user_id, "verifier": verifier}), ex=STATE_TTL_SECONDS)
    try:
        return gmail.build_auth_url(state, challenge, login_hint=login_hint)
    except gmail.GmailNotConfigured as e:
        raise _error(503, "gmail_not_configured", str(e))


def finish_connect(db: Session, *, state: str, code: str | None, error: str | None) -> EmailAccount:
    """Raises ValueError with a short reason the redirect shows to the user."""
    r = get_redis()
    raw = r.get(_state_key(state or ""))
    if not raw:
        raise ValueError("expired")  # unknown, used or older than 10 minutes
    r.delete(_state_key(state))
    data = json.loads(raw)
    if error or not code:
        raise ValueError("denied" if error == "access_denied" else "failed")

    try:
        tokens = gmail.exchange_code(code, data["verifier"])
    except gmail.GmailError:
        raise ValueError("failed")
    granted = set((tokens.get("scope") or "").split())
    if gmail.READONLY not in granted:
        raise ValueError("scope")  # the user unticked mailbox access on the consent screen
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise ValueError("failed")

    try:
        profile = gmail.GmailClient(refresh).profile()
    except (gmail.GmailError, gmail.GrantRevoked):
        raise ValueError("failed")
    address = (profile.get("emailAddress") or "").lower()
    if not address:
        raise ValueError("failed")

    user_id = data["user_id"]
    row = db.query(EmailAccount).filter(EmailAccount.user_id == user_id, EmailAccount.gmail_address == address).first()
    if row is None:
        row = EmailAccount(user_id=user_id, gmail_address=address, connected_at=datetime.now(timezone.utc))
        db.add(row)
    row.token_sealed = key_store.seal(refresh)
    row.scopes = " ".join(sorted(granted))
    row.status = "active"
    row.consecutive_failures = 0
    row.last_error = None
    row.next_sync_at = datetime.now(timezone.utc).replace(tzinfo=None)  # first sync on the next tick
    db.commit()
    logger.info("gmail connected for user=%s account=%s", user_id, row.id)
    return row


def list_accounts(db: Session, user_id: str) -> list[EmailAccount]:
    return (db.query(EmailAccount).filter(EmailAccount.user_id == user_id)
            .order_by(EmailAccount.connected_at).all())


def get_owned(db: Session, user_id: str, account_id: str) -> EmailAccount:
    row = db.query(EmailAccount).filter(EmailAccount.id == account_id, EmailAccount.user_id == user_id).first()
    if row is None:
        raise _error(404, "gmail_account_not_found", "No connected Gmail account with this id.")
    return row


def disconnect(db: Session, account: EmailAccount) -> None:
    try:
        gmail.revoke(key_store.unseal(account.token_sealed))
    except Exception:  # noqa: BLE001 — a broken token must not block disconnecting
        logger.warning("could not revoke the Gmail token for account=%s", account.id)
    db.query(EmailJob).filter(EmailJob.account_id == account.id).delete()
    db.query(EmailEvent).filter(EmailEvent.account_id == account.id).delete()
    db.delete(account)
    db.commit()
