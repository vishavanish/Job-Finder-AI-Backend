"""
app/api/routes/gmail.py
-------------------------
Gmail tracking (CLAUDE.md 7.8, P5):

  - GET    /gmail/connect                  Google consent URL (gmail.readonly only)
  - GET    /gmail/callback                 Google's redirect back; stores the account, then sends the
                                           browser to the app (/tracker?gmail=connected|error)
  - GET    /gmail/accounts                 connected accounts and their sync state
  - DELETE /gmail/accounts/{id}            disconnect: revoke + delete its synced data
  - POST   /gmail/accounts/{id}/sync       sync now (queued)
  - GET    /gmail/events?state=            the job-related emails found (what drove each status change)
  - GET    /gmail/review                   emails the matcher wasn't sure about
  - POST   /gmail/review/{id}/resolve      confirm / change / ignore
"""
from __future__ import annotations

from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.email import EmailAccount, EmailEvent
from app.models.email_schemas import (
    EmailEventList, EmailEventOut, GmailAccountList, GmailAccountOut, GmailConnectOut, ResolveIn,
)
from app.services import email_sync, gmail_accounts
from app.services.entitlements import require_feature

router = APIRouter(prefix="/gmail", tags=["gmail"])


def _account_out(a: EmailAccount) -> GmailAccountOut:
    return GmailAccountOut(id=a.id, gmail_address=a.gmail_address, status=a.status, last_synced_at=a.last_synced_at,
                           next_sync_at=a.next_sync_at, last_error=a.last_error, connected_at=a.connected_at)


def _event_out(e: EmailEvent) -> EmailEventOut:
    return EmailEventOut(id=e.id, account_id=e.account_id, sender=e.sender, subject=e.subject, snippet=e.snippet,
                         received_at=e.received_at, category=e.category, confidence=e.confidence, company=e.company,
                         role=e.role, summary=e.summary, state=e.state, application_id=e.application_id,
                         applied_status=e.applied_status, match_score=e.match_score, suggestions=e.suggestions or [])


def _app_url(path: str, **params: str) -> str:
    base = (get_settings().APP_PUBLIC_URL or "http://localhost:5173").rstrip("/")
    return f"{base}{path}?{urlencode(params)}"


@router.get("/connect", response_model=GmailConnectOut)
def connect(user=Depends(require_feature("gmail_tracking")), db: Session = Depends(get_db)):
    return {"auth_url": gmail_accounts.start_connect(db, user.id, login_hint=user.email)}


@router.get("/callback", include_in_schema=False)
def callback(state: str = "", code: Optional[str] = None, error: Optional[str] = None,
             db: Session = Depends(get_db)):
    """Google redirects the browser here (no bearer token): only the single-use state is trusted."""
    try:
        gmail_accounts.finish_connect(db, state=state, code=code, error=error)
    except ValueError as e:
        return RedirectResponse(_app_url("/tracker", gmail="error", reason=str(e)), status_code=303)
    return RedirectResponse(_app_url("/tracker", gmail="connected"), status_code=303)


@router.get("/accounts", response_model=GmailAccountList)
def accounts(user=Depends(get_current_user), db: Session = Depends(get_db)):
    s = get_settings()
    return {"accounts": [_account_out(a) for a in gmail_accounts.list_accounts(db, user.id)],
            "configured": bool(s.GMAIL_CLIENT_ID and s.GMAIL_CLIENT_SECRET)}


@router.delete("/accounts/{account_id}", status_code=204)
def disconnect(account_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    gmail_accounts.disconnect(db, gmail_accounts.get_owned(db, user.id, account_id))


@router.post("/accounts/{account_id}/sync", status_code=202)
@limiter.limit(lambda: get_settings().RATE_LIMIT_GMAIL_SYNC)
def sync_now(request: Request, account_id: str, user=Depends(require_feature("gmail_tracking")),
             db: Session = Depends(get_db)):
    account = gmail_accounts.get_owned(db, user.id, account_id)
    from app.core.celery_tasks import sync_gmail_account_task
    sync_gmail_account_task.apply_async(args=[account.id], queue=get_settings().email_queue)
    return {"queued": True, "message": "Checking your inbox — new results appear in a minute or two."}


@router.get("/events", response_model=EmailEventList)
def events(state: Optional[Literal["pending", "applied", "needs_review", "ignored", "job_alert"]] = None,
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
           user=Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(EmailEvent).filter(EmailEvent.user_id == user.id)
    if state:
        q = q.filter(EmailEvent.state == state)
    total = q.count()
    rows = q.order_by(EmailEvent.received_at.desc()).offset(offset).limit(limit).all()
    return {"events": [_event_out(e) for e in rows], "total": total}


@router.get("/review", response_model=EmailEventList)
def review(user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(EmailEvent).filter(EmailEvent.user_id == user.id, EmailEvent.state == "needs_review")
            .order_by(EmailEvent.received_at.desc()).limit(200).all())
    return {"events": [_event_out(e) for e in rows], "total": len(rows)}


@router.post("/review/{event_id}/resolve", response_model=EmailEventOut)
def resolve(event_id: str, body: ResolveIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    event = db.query(EmailEvent).filter(EmailEvent.id == event_id, EmailEvent.user_id == user.id).first()
    if event is None:
        raise HTTPException(404, detail={"code": "email_not_found", "message": "No email with this id."})
    return _event_out(email_sync.resolve(db, user.id, event, action=body.action,
                                         application_id=body.application_id, category=body.category))
