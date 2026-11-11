"""
app/services/live_takeover.py
-------------------------------
QA11 A — "Sign in to continue": when an Auto apply run reaches a page only
the user can get past (sign in, create an account, a captcha), it waits in
`awaiting_login` instead of ending at `needs_user`. The user opens a live
view of a browser on the server (the job_finder_browser service), does it
themselves, and the run carries on with that browser's cookies.

We never type, store or see a password field's value on purpose: the user's
keys go through to the page and nothing typed is logged or kept. The
cookies (storage state) are sealed in Redis for LIVE_STATE_TTL_SECONDS and
used only by this run's next steps.

  eligible(db, run)              may this run wait for a live sign-in?
  begin(db, run, kind, reason, url, phase)   -> awaiting_login
  takeover_out(db, run)          RunOut.takeover
  start(db, user, run)           opens (or reuses) the live browser; returns a stream token
  consume_token(token, run_id)   the socket's first message
  proceed(db, run)               cookies -> engine; queued (or submitting) again
  give_up(db, run)               -> needs_user
  expire_waiting(db, user_id)    runs left waiting too long -> needs_user
  storage_state(run_id)          what the engine opens its browser with
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.auth import JWT_ALGORITHM
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.live_browser import internal_key
from app.models.apply import ApplyRun
from app.services import feature_flags, key_store

logger = logging.getLogger("job_finder_api.live_takeover")

TOKEN_SECONDS = 60
KINDS = ("login", "account", "captcha")
KIND_TEXT = {
    "login": "asks you to sign in",
    "account": "asks you to sign in or create an account",
    "captcha": "shows a check only a person can do",
}


def _error(code: int, err: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": err, "message": message})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- eligibility

def configured() -> bool:
    return bool(get_settings().LIVE_BROWSER_URL)


def switched_on(db: Session) -> bool:
    return configured() and bool(feature_flags.get_flag(db, "live_takeover_enabled"))


def eligible(db: Session, run: ApplyRun, kind: str | None) -> bool:
    """Server-side Auto apply runs only (the extension and Fast apply already run in the user's own browser),
    a stop the user can get past, the switch on, and not too many takeovers for this run already."""
    plan = run.plan or {}
    return (kind in KINDS and run.mode == "auto" and (plan.get("runner") or "worker") == "worker"
            and switched_on(db) and int(plan.get("takeovers") or 0) < get_settings().LIVE_MAX_TAKEOVERS_PER_RUN)


def _day_key(user_id: str) -> str:
    return f"live_takeover:day:{user_id}:{_now():%Y%m%d}"


def sessions_left_today(user_id: str) -> int:
    used = int(get_redis().get(_day_key(user_id)) or 0)
    return max(0, get_settings().LIVE_SESSION_DAILY_CAP - used)


# ---------------------------------------------------------------- the stop

def begin(db: Session, run: ApplyRun, *, kind: str, reason: str, url: str, phase: str) -> dict:
    """Records what the page asks for; the caller moves the run to awaiting_login."""
    plan = dict(run.plan or {})
    plan["takeover"] = {"kind": kind, "reason": reason, "url": url or run.apply_url, "phase": phase,
                        "started_at": _now().isoformat()}
    plan["takeovers"] = int(plan.get("takeovers") or 0) + 1
    run.plan = plan
    db.commit()
    host = urlparse(url or run.apply_url).hostname or "The site"
    return {"message": f"{host} {KIND_TEXT[kind]}. Open this run and do it on the live view — "
                       f"Auto apply carries on after."}


def takeover_out(db: Session, run: ApplyRun) -> dict | None:
    t = (run.plan or {}).get("takeover")
    if run.state != "awaiting_login" or not t:
        return None
    started = _parse(t["started_at"])
    return {"kind": t["kind"], "site": urlparse(t.get("url") or run.apply_url).hostname or "",
            "reason": t["reason"], "started_at": started,
            "expires_at": started + timedelta(minutes=get_settings().LIVE_WAIT_MINUTES),
            "available": switched_on(db), "sessions_left_today": sessions_left_today(run.user_id)}


def expire_waiting(db: Session, user_id: str) -> None:
    from app.services.apply_engine import _transition
    limit = timedelta(minutes=get_settings().LIVE_WAIT_MINUTES)
    for run in db.query(ApplyRun).filter(ApplyRun.user_id == user_id, ApplyRun.state == "awaiting_login").all():
        t = (run.plan or {}).get("takeover") or {}
        if not t.get("started_at") or _now() - _parse(t["started_at"]) > limit:
            close_session(run.id)
            _transition(db, run, "needs_user", "Nobody signed in on the live view in time. Finish this application "
                                               "on the job page.", tracker_status="needs_user")


# ---------------------------------------------------------------- the service

def _client() -> httpx.Client:
    return httpx.Client(base_url=get_settings().LIVE_BROWSER_URL, timeout=60,
                        headers={"X-Live-Key": internal_key()})


def _call(method: str, path: str, **kwargs) -> httpx.Response:
    """One call to the live browser service (seam for tests)."""
    with _client() as c:
        return c.request(method, path, **kwargs)


def close_session(run_id: str) -> None:
    if not configured():
        return
    try:
        _call("DELETE", f"/sessions/{run_id}")
    except httpx.HTTPError:
        pass


def _need_waiting(db: Session, run: ApplyRun) -> dict:
    if run.state != "awaiting_login" or not (run.plan or {}).get("takeover"):
        raise _error(409, "run_not_waiting_login", "This run is not waiting for you to sign in.")
    if not switched_on(db):
        raise _error(503, "live_off", "Signing in on a live view is switched off. Finish it on the job page.")
    return run.plan["takeover"]


def start(db: Session, run: ApplyRun) -> dict:
    t = _need_waiting(db, run)
    if sessions_left_today(run.user_id) <= 0:
        raise _error(429, "live_daily_cap", "You've used today's live sign-ins. Finish this one on the job page.")
    from app.apply import sites
    allowed = sorted({sites.host_of(t.get("url") or ""), sites.host_of(run.apply_url)} - {""})
    try:
        resp = _call("POST", "/sessions", json={"session_id": run.id, "url": t.get("url") or run.apply_url,
                                                "allowed_sites": allowed, "storage_state": storage_state(run.id)})
    except httpx.HTTPError:
        raise _error(503, "live_unavailable", "The live browser isn't reachable right now. Try again in a minute, "
                                              "or finish it on the job page.")
    if resp.status_code == 409:
        raise _error(409, "live_busy", "All live browsers are in use. Try again in a minute.")
    if resp.status_code >= 400:
        raise _error(503, "live_unavailable", "The live browser could not open this page. Try again, or finish "
                                              "it on the job page.")
    body = resp.json()
    if body.get("created"):
        r = get_redis()
        r.incr(_day_key(run.user_id))
        r.expire(_day_key(run.user_id), 2 * 86400)
    cfg = get_settings()
    return {"token": issue_token(run.user_id, run.id), "expires_in": TOKEN_SECONDS,
            "width": body.get("width", 1280), "height": body.get("height", 860),
            "session_minutes": cfg.LIVE_SESSION_MINUTES}


# ---------------------------------------------------------------- stream tokens (like voice_auth)

def issue_token(user_id: str, run_id: str) -> str:
    jti = uuid.uuid4().hex
    now = _now()
    get_redis().set(f"live_tok:{jti}", run_id, ex=TOKEN_SECONDS)
    return jwt.encode({"sub": user_id, "rid": run_id, "jti": jti, "typ": "live_stream", "iat": int(now.timestamp()),
                       "exp": now + timedelta(seconds=TOKEN_SECONDS)},
                      get_settings().JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def consume_token(token: str, run_id: str) -> dict | None:
    try:
        claims = jwt.decode(token or "", get_settings().JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    if claims.get("typ") != "live_stream" or claims.get("rid") != run_id or not claims.get("jti"):
        return None
    if not get_redis().delete(f"live_tok:{claims['jti']}"):
        return None
    return claims


def connect_service(run_id: str):
    """The upstream socket to the live browser service (seam for tests)."""
    import websockets
    return websockets.connect(stream_url(run_id), additional_headers={"X-Live-Key": internal_key()},
                              max_size=4 * 1024 * 1024, open_timeout=10)


def stream_url(run_id: str) -> str:
    base = get_settings().LIVE_BROWSER_URL.rstrip("/")
    return base.replace("https://", "wss://").replace("http://", "ws://") + f"/sessions/{run_id}/stream"


# ---------------------------------------------------------------- after the sign-in

def _state_key(run_id: str) -> str:
    return f"apply_live_state:{run_id}"


def storage_state(run_id: str) -> dict | None:
    sealed = get_redis().get(_state_key(run_id))
    if not sealed:
        return None
    try:
        return json.loads(key_store.unseal(sealed))
    except Exception:  # noqa: BLE001 — a stale or broken value just means "no sign-in"
        return None


def proceed(db: Session, run: ApplyRun) -> ApplyRun:
    """The user is signed in (or solved the check): keep the browser's cookies for this run and let the engine
    carry on from where it stopped."""
    from app.services.apply_engine import _transition
    t = _need_waiting(db, run)
    try:
        resp = _call("POST", f"/sessions/{run.id}/finish")
    except httpx.HTTPError:
        raise _error(503, "live_unavailable", "The live browser isn't reachable right now. Try again in a minute.")
    if resp.status_code == 404:
        raise _error(409, "live_ended", "The live view had already closed, so the sign-in was lost. Open it again "
                                        "and sign in once more.")
    if resp.status_code >= 400:
        raise _error(503, "live_unavailable", "Couldn't take over the signed-in browser. Try again.")
    state = resp.json().get("storage_state") or {}
    get_redis().set(_state_key(run.id), key_store.seal(json.dumps(state)), ex=get_settings().LIVE_STATE_TTL_SECONDS)
    plan = dict(run.plan or {})
    plan["takeover_done"] = {"kind": t["kind"], "at": _now().isoformat()}
    plan.pop("takeover", None)
    run.plan = plan
    db.commit()
    if t.get("phase") == "submit":
        _transition(db, run, "submitting", "Signed in — submitting now.", tracker_status="submitting")
    else:
        _transition(db, run, "queued", "Signed in — reading the application form again.", tracker_status="queued")
    return run


def give_up(db: Session, run: ApplyRun) -> ApplyRun:
    from app.services.apply_engine import _transition
    if run.state != "awaiting_login":
        raise _error(409, "run_not_waiting_login", "This run is not waiting for you to sign in.")
    close_session(run.id)
    _transition(db, run, "needs_user", "You chose to finish this application on the job page yourself.",
                tracker_status="needs_user")
    return run


def health() -> dict | None:
    if not configured():
        return None
    try:
        resp = _call("GET", "/health")
        return resp.json() if resp.status_code == 200 else {"error": f"HTTP {resp.status_code}"}
    except httpx.HTTPError as e:
        return {"error": type(e).__name__}
