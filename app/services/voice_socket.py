"""
app/services/voice_socket.py
------------------------------
Plumbing shared by the two voice sockets (Practice: practice_session.py,
Live Copilot: copilot_session.py; P7 plan 2.6):

  - close codes;
  - VoiceSocket: serialized sends, status/error events, task ownership
    (every task a session starts is cancelled by close()), elapsed time;
  - first-message auth (accept, wait ≤ 5 s for {"type": "auth"}, consume the
    single-use stream token) and the one-live-session lock;
  - schedule_finalize(): the Celery job that ends/evaluates a session.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.auth import as_utc
from app.core.config import get_settings
from app.services import voice_auth

CLOSE_AUTH = 4401
CLOSE_COST_CAP = 4402
CLOSE_FORBIDDEN = 4403
CLOSE_ENDED = 4404
CLOSE_BUSY = 4409
CLOSE_TOO_BIG = 1009

GATE_CODES = {"auth": (CLOSE_AUTH, "auth_failed"), "ended": (CLOSE_ENDED, "session_ended"),
              "forbidden": (CLOSE_FORBIDDEN, "feature_disabled"),
              "ack": (CLOSE_FORBIDDEN, "copilot_ack_required")}


class VoiceSocket:
    """Base for one voice session on one socket."""

    def __init__(self, ws: WebSocket, *, session_id: str, user_id: str, started_at: datetime | None,
                 cost_usd: float, cost_cap: float):
        self.ws = ws
        self.session_id = session_id
        self.user_id = user_id
        self.cost = float(cost_usd or 0.0)
        self.cost_cap = float(cost_cap or 0.0)
        self.free_only = self.cost_cap > 0 and self.cost >= self.cost_cap
        self.elapsed_before = 0.0
        if started_at is not None:
            self.elapsed_before = max(0.0, (datetime.now(timezone.utc) - as_utc(started_at)).total_seconds())
        self.opened = time.monotonic()
        self.tasks: list[asyncio.Task] = []
        self.send_lock = asyncio.Lock()
        self.ended = False

    def _elapsed(self) -> float:
        return self.elapsed_before + (time.monotonic() - self.opened)

    async def send(self, payload: dict | bytes) -> None:
        if self.ws.application_state != WebSocketState.CONNECTED:
            return
        async with self.send_lock:
            try:
                if isinstance(payload, bytes):
                    await self.ws.send_bytes(payload)
                else:
                    await self.ws.send_text(json.dumps(payload, default=str))
            except Exception:  # noqa: BLE001 — the socket went away; run() notices on its next receive
                pass

    async def status(self, state: str, **extra) -> None:
        await self.send({"type": "status", "state": state, **extra})

    async def error(self, code: str, message: str, *, retryable: bool) -> None:
        await self.send({"type": "error", "code": code, "message": message, "retryable": retryable})

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        task.add_done_callback(lambda t: self.tasks.remove(t) if t in self.tasks else None)
        return task

    async def cancel_tasks(self) -> None:
        """Cancels every task this session started, except the caller's own
        (a task that ends the session). Safe to call twice."""
        current = asyncio.current_task()
        others = [t for t in list(self.tasks) if t is not current]
        for task in others:
            task.cancel()
        if others:
            await asyncio.gather(*others, return_exceptions=True)


async def reject(ws: WebSocket, code: int, err: str, message: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "code": err, "message": message, "retryable": False}))
        await ws.close(code=code)
    except Exception:  # noqa: BLE001
        pass


async def authenticate(ws: WebSocket, session_id: str) -> dict | None:
    """Accepts the socket and checks the first message. Claims, or None
    after rejecting the socket."""
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=5)
    except asyncio.TimeoutError:
        await reject(ws, CLOSE_AUTH, "auth_required", "Send {\"type\": \"auth\", \"token\": ...} first.")
        return None
    if first["type"] == "websocket.disconnect":
        return None
    try:
        msg = json.loads(first.get("text") or "")
    except ValueError:
        msg = {}
    claims = voice_auth.consume(str(msg.get("token") or ""), session_id) if msg.get("type") == "auth" else None
    if claims is None:
        await reject(ws, CLOSE_AUTH, "auth_failed", "This stream token is invalid, expired or already used.")
    return claims


async def reject_gate(ws: WebSocket, err: str, message: str) -> None:
    code, name = GATE_CODES[err]
    await reject(ws, code, name, message)


async def lock_or_reject(ws: WebSocket, user_id: str, session_id: str) -> bool:
    if voice_auth.acquire_live(user_id, session_id):
        return True
    await reject(ws, CLOSE_BUSY, "voice_session_busy",
                 "Another voice session is open. End it first (only one at a time).")
    return False


def schedule_finalize(session_id: str, countdown: int | None = None, attempt: int = 0) -> None:
    from app.core.celery_tasks import voice_finalize_task
    opts = {"countdown": countdown} if countdown else {}
    if attempt:
        opts["kwargs"] = {"attempt": attempt}
    voice_finalize_task.apply_async(args=[session_id], queue=get_settings().default_queue, **opts)
