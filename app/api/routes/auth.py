"""
app/api/routes/auth.py
-------------------------
Sign-in (CLAUDE.md 7.1). Every successful sign-in returns a 15-minute
access token in the body and sets a rotating refresh token as an
httpOnly cookie (see app/services/sessions.py). The frontend sends
`Authorization: Bearer <access_token>` and calls POST /auth/refresh
(with credentials) when it gets a 401.

  - POST /auth/otp/request, /auth/otp/verify  — email one-time code
  - POST /auth/google                         — Google ID token
  - POST /auth/register, /auth/login          — password (legacy path)
  - POST /auth/refresh, /auth/logout, GET /auth/me, POST /auth/password

Unknown emails can only create accounts while signup is open (feature
flag invite_only); in invite-only mode admins create accounts.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, hash_password, verify_password
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.schemas import (
    EntitlementOut, EntitlementSummary, GoogleSignIn, LoginRequest, MeOut, MessageResponse, OtpRequest, OtpVerify, RegisterRequest,
    SetPasswordRequest, TokenResponse, UserOut,
)
from app.models.user import User
from app.providers import google_identity
from app.providers.email import EmailNotConfigured, EmailSender, get_email_sender
from app.services import accounts, audit, entitlements, otp, sessions

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("job_finder_api.auth")
settings = get_settings()

OTP_SENT_MESSAGE = "If this address can sign in, a code has been sent. It expires in 10 minutes."


# ============================================================
# Email one-time code
# ============================================================

def _send_code(sender: EmailSender, email: str, code: str) -> None:
    try:
        sender.send(
            to=email,
            subject=f"Your Job Finder sign-in code: {code}",
            body=(
                f"Your sign-in code is {code}.\n\n"
                "It expires in 10 minutes and can be used once. "
                "If you didn't ask for it, you can ignore this email."
            ),
        )
    except Exception:  # noqa: BLE001 — runs after the response; nothing to return to
        logger.exception("failed to send OTP email")


@router.post("/otp/request", response_model=MessageResponse, status_code=202)
@limiter.limit(settings.RATE_LIMIT_AUTH)
def request_otp(request: Request, req: OtpRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Emails a 6-digit code. The response is identical whether or not the
    address has (or may create) an account."""
    try:
        sender = get_email_sender()
    except EmailNotConfigured as e:
        logger.error("OTP request refused: %s", e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email sign-in is not available right now.")

    try:
        code = otp.issue_code(req.email)
    except otp.OtpCooldown as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={"code": "otp_cooldown", "retry_after": e.retry_after,
                                    "message": "Please wait before requesting another code."},
                            headers={"Retry-After": str(e.retry_after)})
    except otp.OtpTooManyRequests:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={"code": "otp_too_many_requests",
                                    "message": "Too many codes requested. Try again in an hour."})

    user = accounts.find_by_email(db, req.email)
    may_sign_in = (user is not None and user.is_active) or (user is None and accounts.signup_open(db))
    if may_sign_in:
        background.add_task(_send_code, sender, otp.normalize_email(req.email), code)
    return MessageResponse(detail=OTP_SENT_MESSAGE)


_OTP_ERRORS = {
    "invalid": lambda left: {
        "code": "otp_invalid", "attempts_left": left,
        "message": f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left.",
    },
    "locked": lambda _: {
        "code": "otp_locked", "attempts_left": 0,
        "message": "Too many wrong attempts. Request a new code.",
    },
    "expired": lambda _: {
        "code": "otp_expired",
        "message": "This code has expired or was already used. Request a new code.",
    },
}


@router.post("/otp/verify", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
def verify_otp(request: Request, req: OtpVerify, response: Response, db: Session = Depends(get_db)):
    result = otp.check_code(req.email, req.code)
    if not result.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=_OTP_ERRORS[result.status](result.attempts_left))
    user = accounts.sign_in_verified(db, email=req.email, provider="email_otp", request=request)
    return sessions.start_session(user, response, db)


# ============================================================
# Google
# ============================================================

@router.post("/google", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
def google_sign_in(request: Request, req: GoogleSignIn, response: Response, db: Session = Depends(get_db)):
    try:
        claims = google_identity.verify_id_token(req.id_token)
    except google_identity.GoogleNotConfigured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in is not configured.")
    except google_identity.GoogleTokenInvalid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google sign-in failed. Please try again.")

    if not claims.get("email") or claims.get("email_verified") is not True:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your Google account email is not verified.")

    user = accounts.sign_in_verified(
        db, email=claims["email"], provider="google", request=request,
        google_sub=claims["sub"], name=claims.get("name"),
    )
    return sessions.start_session(user, response, db)


# ============================================================
# Password (legacy path, kept for existing accounts)
# ============================================================

@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit(settings.RATE_LIMIT_AUTH)
def register(request: Request, req: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    if not accounts.is_always_admin(req.email) and not accounts.signup_open(db):
        logger.warning("blocked self-registration attempt (invite-only)")
        raise accounts.SignupClosed()

    normalized_email = otp.normalize_email(req.email)
    if accounts.find_by_email(db, normalized_email):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    user = User(email=normalized_email, hashed_password=hash_password(req.password), name=req.name,
                auth_provider="password", role="admin" if accounts.is_always_admin(normalized_email) else "user")
    db.add(user)
    db.flush()
    audit.record(db, actor_id=user.id, action="user.signup", target_type="user", target_id=user.id,
                 after={"email": normalized_email, "provider": "password"}, request=request)
    logger.info("registered new user: %s", user.id)
    response.status_code = status.HTTP_201_CREATED
    return sessions.start_session(user, response, db)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
def login(request: Request, req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = accounts.find_by_email(db, req.email)
    if not user or not verify_password(req.password, user.hashed_password):
        logger.warning("failed password login attempt")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")
    if not user.is_active:
        raise accounts.AccountDisabled()

    audit.record(db, actor_id=user.id, action="auth.login", target_type="user", target_id=user.id,
                 after={"provider": "password"}, request=request)
    return sessions.start_session(user, response, db)


@router.post("/password", response_model=MessageResponse)
def set_password(request: Request, req: SetPasswordRequest, user=Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Lets an OTP/Google user add a password (or change their own)."""
    user = db.merge(user)
    user.hashed_password = hash_password(req.password)
    audit.record(db, actor_id=user.id, action="auth.password_set", target_type="user", target_id=user.id,
                 request=request)
    db.commit()
    return MessageResponse(detail="Password updated.")


# ============================================================
# Session lifecycle
# ============================================================

@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    """Exchanges the refresh cookie for a new access token (and a new
    refresh cookie). Call with credentials included."""
    return sessions.rotate(request.cookies.get(settings.REFRESH_COOKIE_NAME), response, db, request)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    sessions.end_session(request.cookies.get(settings.REFRESH_COOKIE_NAME), response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=MeOut)
def me(user=Depends(get_current_user), db: Session = Depends(get_db)):
    summaries = {}
    for e in entitlements.all_effective(db, user.id):
        remaining = None
        if e.quota_per_day is not None:
            remaining = max(e.quota_per_day - entitlements.usage_today(user.id, e.feature), 0)
        summaries[e.feature] = EntitlementSummary(enabled=e.enabled, max_level=e.max_level,
                                                  quota_per_day=e.quota_per_day, quota_remaining=remaining)
    return MeOut(**UserOut.model_validate(user).model_dump(), entitlements=summaries)


@router.get("/me/entitlements", response_model=list[EntitlementOut])
def my_entitlements(user=Depends(get_current_user), db: Session = Depends(get_db)):
    """What this account can use — lets the UI disable buttons and say why."""
    return [
        EntitlementOut(**e.as_dict(), used_today=entitlements.usage_today(user.id, e.feature))
        for e in entitlements.all_effective(db, user.id)
    ]
