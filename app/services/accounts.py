"""
app/services/accounts.py
---------------------------
Turning a *verified* email (a correct OTP, or a Google token with
email_verified) into a signed-in user. One place enforces the account
rules for every sign-in method:

  - a disabled account cannot sign in;
  - an unknown email may only create an account when signup is open
    (feature flag invite_only = false);
  - email_verified_at is stamped on every verified sign-in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.models.user import User
from app.services import audit, feature_flags
from app.services.otp import normalize_email

logger = logging.getLogger("job_finder_api.accounts")


class SignupClosed(HTTPException):
    def __init__(self):
        super().__init__(status.HTTP_403_FORBIDDEN, "Sign-up is by invitation only. Ask an admin for access.")


class AccountDisabled(HTTPException):
    """Same body as core.auth.account_disabled(), so every path that meets
    a disabled account answers 403 {code: "account_disabled"}."""
    def __init__(self):
        super().__init__(status.HTTP_403_FORBIDDEN,
                         {"code": "account_disabled", "message": "This account has been disabled. Contact your admin."})


def find_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == normalize_email(email)).first()


def signup_open(db: Session) -> bool:
    return not feature_flags.get_flag(db, "invite_only")


def sign_in_verified(
    db: Session,
    *,
    email: str,
    provider: str,
    request: Request,
    google_sub: str | None = None,
    name: str | None = None,
) -> User:
    """Returns the user to start a session for. Does not commit — the
    caller's start_session() commits the user, last_login_at and the
    audit entry together."""
    email = normalize_email(email)
    user = None
    if google_sub:
        user = db.query(User).filter(User.google_sub == google_sub).first()
    user = user or find_by_email(db, email)

    if user is None:
        if not signup_open(db):
            logger.info("sign-in refused: unknown email in invite-only mode (provider=%s)", provider)
            raise SignupClosed()
        user = User(email=email, name=name, auth_provider=provider, role="user")
        db.add(user)
        db.flush()
        audit.record(db, actor_id=user.id, action="user.signup", target_type="user", target_id=user.id,
                     after={"email": email, "provider": provider}, request=request)

    if not user.is_active:
        raise AccountDisabled()

    user.email_verified_at = datetime.now(timezone.utc)
    if google_sub and not user.google_sub:
        user.google_sub = google_sub
    if name and not user.name:
        user.name = name
    audit.record(db, actor_id=user.id, action="auth.login", target_type="user", target_id=user.id,
                 after={"provider": provider}, request=request)
    return user
