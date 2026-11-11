"""
app/live_browser/service.py
-----------------------------
The live browser service (QA11 A). Localhost only; every call needs the
`X-Live-Key` header (app.live_browser.internal_key()).

  POST   /sessions                 {session_id, url, allowed_sites[], storage_state?} -> {created, width, height}
                                   (200 + created false when it already runs; 409 live_busy when full)
  WS     /sessions/{id}/stream     frames (binary JPEG) + JSON events out; input JSON in
  POST   /sessions/{id}/finish     -> {storage_state}; closes the session
  DELETE /sessions/{id}
  GET    /health                   {sessions, max}

Sessions close after LIVE_SESSION_MINUTES, or LIVE_IDLE_MINUTES without input.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.live_browser import internal_key
from app.live_browser.session import HEIGHT, WIDTH, LiveSession

logger = logging.getLogger("job_finder_api.live_browser")

SESSIONS: dict[str, LiveSession] = {}
_LOCK = asyncio.Lock()


class StartIn(BaseModel):
    session_id: str = Field(..., max_length=64)
    url: str = Field(..., max_length=4000)
    allowed_sites: list[str] = Field(default_factory=list, max_length=20)
    storage_state: dict | None = None


def _check(key: str | None) -> None:
    if not key or not hmac.compare_digest(key, internal_key()):
        raise HTTPException(401, detail={"code": "bad_key", "message": "Unknown caller."})


async def _reaper() -> None:
    cfg = get_settings()
    while True:
        await asyncio.sleep(10)
        now = time.monotonic()
        for sid, s in list(SESSIONS.items()):
            if s.closed or now - s.started > cfg.LIVE_SESSION_MINUTES * 60 \
                    or now - s.last_input > cfg.LIVE_IDLE_MINUTES * 60:
                SESSIONS.pop(sid, None)
                logger.info("live session %s ended (time)", sid)
                await s.close()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        task.cancel()
        for s in list(SESSIONS.values()):
            await s.close()
        SESSIONS.clear()


app = FastAPI(title="Job Finder live browser", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health(x_live_key: str | None = Header(None)):
    _check(x_live_key)
    return {"sessions": len(SESSIONS), "max": get_settings().LIVE_SESSIONS_MAX}


@app.post("/sessions")
async def start(body: StartIn, x_live_key: str | None = Header(None)):
    _check(x_live_key)
    cfg = get_settings()
    async with _LOCK:
        existing = SESSIONS.get(body.session_id)
        if existing is not None and not existing.closed:
            return {"created": False, "width": WIDTH, "height": HEIGHT}
        if len(SESSIONS) >= cfg.LIVE_SESSIONS_MAX:
            raise HTTPException(409, detail={"code": "live_busy",
                                             "message": "All live browsers are in use. Try again in a minute."})
        session = LiveSession(body.session_id, body.url, set(body.allowed_sites), storage_state=body.storage_state,
                              allow_private=cfg.LIVE_ALLOW_PRIVATE, fps=cfg.LIVE_FPS)
        SESSIONS[body.session_id] = session
    try:
        await session.start()
    except Exception as e:  # noqa: BLE001
        SESSIONS.pop(body.session_id, None)
        await session.close()
        logger.warning("live session %s failed to start: %s", body.session_id, type(e).__name__)
        raise HTTPException(503, detail={"code": "live_failed", "message": "The live browser could not start."})
    return {"created": True, "width": WIDTH, "height": HEIGHT}


@app.post("/sessions/{session_id}/finish")
async def finish(session_id: str, x_live_key: str | None = Header(None)):
    _check(x_live_key)
    session = SESSIONS.pop(session_id, None)
    if session is None or session.closed:
        raise HTTPException(404, detail={"code": "live_ended", "message": "This live session has ended."})
    return {"storage_state": await session.finish(), "ready": session.ready}


@app.delete("/sessions/{session_id}", status_code=204)
async def stop(session_id: str, x_live_key: str | None = Header(None)):
    _check(x_live_key)
    session = SESSIONS.pop(session_id, None)
    if session is not None:
        await session.close()


@app.websocket("/sessions/{session_id}/stream")
async def stream(ws: WebSocket, session_id: str):
    key = ws.headers.get("x-live-key")
    if not key or not hmac.compare_digest(key, internal_key()):
        await ws.close(code=4401)
        return
    session = SESSIONS.get(session_id)
    if session is None or session.closed:
        await ws.close(code=4404)
        return
    await ws.accept()

    async def send(msg) -> None:
        if isinstance(msg, bytes):
            await ws.send_bytes(msg)
        else:
            await ws.send_text(json.dumps(msg))

    async def receive() -> dict | None:
        try:
            raw = await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return None
        try:
            msg = json.loads(raw)
        except ValueError:
            return {}
        return msg if isinstance(msg, dict) else {}

    await session.stream(send, receive)
    with contextlib.suppress(Exception):
        await ws.close(code=1000 if not session.closed else 4410)
