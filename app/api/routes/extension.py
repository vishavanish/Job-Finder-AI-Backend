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

  Fast apply in the user's own browser (P8b E3a; needs auto_apply + Apply settings "extension_assist"):
  - GET    /extension/runs/for-page?url=   the Fast apply run started in the app for this LinkedIn job
  - POST   /extension/runs/{id}/fill       answers for the fields on one Easy Apply page
  - POST   /extension/runs/{id}/answer     the user answered a question in the extension's panel
  - GET    /extension/runs/{id}/resume     the resume file to attach (tailored when ready, else master)
  - POST   /extension/runs/{id}/result     submitted (LinkedIn confirmed) | closed (tab closed first)

  "Fill this form" on a page with no run (P8b E3b; same gates):
  - POST   /extension/page/fill            answers for the fields on the page
  - POST   /extension/page/answer          the user's answer from the panel → answer bank
  - GET    /extension/page/resume          the master resume file
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.extension_schemas import (
    AssistFillIn, AssistFillOut, AssistPageAnswerIn, AssistRememberIn, AssistResultIn, AssistResultOut, AssistRunOut, CaptureIn,
    CaptureOut, ConnectionsOut, ExtensionMeOut, PairCodeOut, TokenIn, TokenOut,
)
from app.models.jobs import Job
from app.services import entitlements, extension, extension_assist
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


# ---------------------------------------------------------------- Fast apply in the browser (P8b E3a)

@router.get("/runs/for-page", response_model=AssistRunOut)
def run_for_page(url: str = Query(..., max_length=2000), authorization: str | None = Header(default=None),
                 db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    run = extension_assist.run_for_page(db, user.id, url)
    if run is None:
        return {"run_id": None}
    job = db.get(Job, run.job_id)
    return {"run_id": run.id, "title": job.title if job else "", "company": job.company if job else ""}


@router.post("/runs/{run_id}/fill", response_model=AssistFillOut)
@limiter.limit(lambda: get_settings().RATE_LIMIT_EXTENSION_FILL)
def fill(request: Request, run_id: str, body: AssistFillIn, authorization: str | None = Header(default=None),
         db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    run = extension_assist.get_run(db, user.id, run_id)
    return extension_assist.fill(db, run, [f.model_dump() for f in body.fields])


@router.post("/runs/{run_id}/answer")
def answer(run_id: str, body: AssistRememberIn, authorization: str | None = Header(default=None),
           db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    return extension_assist.remember(db, extension_assist.get_run(db, user.id, run_id), body.key, body.value)


def _file_response(data: bytes, name: str, media: str | None) -> Response:
    return Response(data, media_type=media or "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                             "Cache-Control": "private, no-store"})


@router.get("/runs/{run_id}/resume", response_class=Response,
            responses={200: {"content": {"application/pdf": {}, "application/octet-stream": {}}}})
def resume(run_id: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    return _file_response(*extension_assist.resume_file(db, extension_assist.get_run(db, user.id, run_id)))


@router.post("/runs/{run_id}/result", response_model=AssistResultOut)
def result(run_id: str, body: AssistResultIn, authorization: str | None = Header(default=None),
           db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    run = extension_assist.finish(db, extension_assist.get_run(db, user.id, run_id), body.outcome)
    return {"run_id": run.id, "state": run.state}


# ---------------------------------------------------------------- "Fill this form" with no run (P8b E3b)

@router.post("/page/fill", response_model=AssistFillOut)
@limiter.limit(lambda: get_settings().RATE_LIMIT_EXTENSION_FILL)
def page_fill(request: Request, body: AssistFillIn, authorization: str | None = Header(default=None),
              db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    return extension_assist.fill_page(db, user.id, [f.model_dump() for f in body.fields])


@router.post("/page/answer")
def page_answer(body: AssistPageAnswerIn, authorization: str | None = Header(default=None),
                db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    return extension_assist.remember_page(db, user.id, body.label, body.value, body.options)


@router.get("/page/resume", response_class=Response,
            responses={200: {"content": {"application/pdf": {}, "application/octet-stream": {}}}})
def page_resume(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    user = extension_assist.assist_user(db, _bearer(authorization))
    return _file_response(*extension_assist.page_resume(db, user.id))
