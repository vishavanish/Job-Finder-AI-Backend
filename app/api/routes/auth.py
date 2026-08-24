"""
app/api/routes/auth.py
-------------------------
Real user accounts: register + login, issuing a JWT the frontend stores
and sends as `Authorization: Bearer <token>` on subsequent requests.
Backed by Postgres (Supabase) via app/core/db.py — same engine/session
used by every other route in this app.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.auth import hash_password, verify_password, create_access_token
from app.models.user import User
from app.models.schemas import RegisterRequest, LoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("job_finder_api.auth")


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    normalized_email = req.email.lower().strip()
    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")
    
    # logger.warning("blocked self-registration attempt for email=%s", req.email.lower().strip())
    # raise HTTPException(
    #     status.HTTP_403_FORBIDDEN,
    #     "Self-registration is disabled. Please contact the admin to request an account.",
    # )

    user = User(
        email=normalized_email,
        hashed_password=hash_password(req.password),
        name=req.name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(subject=user.id)
    logger.info("registered new user: %s", user.id)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    normalized_email = req.email.lower().strip()
    user = db.query(User).filter(User.email == normalized_email).first()

    if not user or not verify_password(req.password, user.hashed_password):
        logger.warning("failed login attempt for email=%s", normalized_email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been deactivated.")

    token = create_access_token(subject=user.id)
    logger.info("login: user=%s", user.id)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))