"""
app/api/routes/extension.py
-----------------------------
Browser extension "Job Finder capture" (P8a E1):

  Signed in to the web app (normal JWT):
  - POST   /extension/pair                 a 6-digit pairing code (entitlement browser_capture)
  - GET    /extension/connections          connected extensions + how many jobs were saved
  - DELETE /extension/connections/{id}     disconnect one

  From the extension:
  - POST   /extension/token {code}         trade the code for a capture-only token (rate limited)
  - GET    /extension/me                   who the token belongs to (Authorization: Bearer jfx_…)
  - POST   /extension/captures             save the jobs on the page the user is looking at
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.extension_schemas import (
    CaptureIn, CaptureOut, ConnectionsOut, ExtensionMeOut, PairCodeOut, TokenIn, TokenOut,
)
from app.services import entitlements, extension
from app.services.entitlements import require_feature

router = APIRouter(prefix="/extension", tags=["extension"])


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _capture_user(authorization: str | None, db: Session):
    user, _ = extension.user_for_token(db, _bearer(authorization))
    if not entitlements.effective(db, user.id, "browser_capture").enabled:
        raise extension._error(403, "feature_disabled", "Saving jobs from your browser is not enabled for this account.")
    return user


@router.post("/pair", response_model=PairCodeOut)
@limiter.limit(lambda: get_settings().RATE_LIMIT_EXTENSION_PAIR)
def pair(request: Request, user=Depends(require_feature("browser_capture"))):
    code, seconds = extension.create_pair_code(user.id)
    return {"code": code, "expires_in": seconds}


@router.post("/token", response_model=TokenOut)
@limiter.limit(lambda: get_settings().RATE_LIMIT_EXTENSION_PAIR)
def token(request: Request, body: TokenIn, db: Session = Depends(get_db)):
    raw, row = extension.exchange_code(db, body.code, body.name)
    from app.models.user import User
    return {"token": raw, "id": row.id, "email": db.get(User, row.user_id).email}


@router.get("/connections", response_model=ConnectionsOut)
def connections(user=Depends(get_current_user), db: Session = Depends(get_db)):
    counts = extension.stats(db, user.id)
    return {"connections": [{"id": t.id, "name": t.name, "created_at": t.created_at, "last_used_at": t.last_used_at}
                            for t in extension.list_tokens(db, user.id)],
            "saved_total": counts["total"], "saved_today": counts["today"]}


@router.delete("/connections/{token_id}", status_code=204)
def disconnect(token_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    extension.revoke(db, user.id, token_id)


@router.get("/me", response_model=ExtensionMeOut)
def me(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    user = _capture_user(authorization, db)
    counts = extension.stats(db, user.id)
    return {"email": user.email, "saved_total": counts["total"], "saved_today": counts["today"]}


@router.post("/captures", response_model=CaptureOut)
@limiter.limit(lambda: get_settings().RATE_LIMIT_EXTENSION_CAPTURE)
def captures(request: Request, body: CaptureIn, authorization: str | None = Header(default=None),
             db: Session = Depends(get_db)):
    user = _capture_user(authorization, db)
    return extension.store_captures(db, user.id, body.site.strip().lower(), body.page_url,
                                    [j.model_dump() for j in body.jobs])
