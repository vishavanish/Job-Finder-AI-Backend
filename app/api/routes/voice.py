"""
app/api/routes/voice.py
-------------------------
Voice interview prep (CLAUDE.md 7.10; Practice P7a, Live Copilot P7b):

  - GET    /voice/providers              which speech / AI models a session would use, expected latency
  - GET    /voice/settings               voice choices + the Live Copilot acknowledgement text and state
  - PUT    /voice/settings
  - POST   /voice/copilot/acknowledge    accept the Live Copilot notice (current version; audited)
  - POST   /voice/sessions               start a Practice (mode mock) or Live Copilot session
                                         -> {session, stream_token, stream_path}
  - POST   /voice/sessions/{id}/token    a fresh single-use stream token (reconnect)
  - WS     /voice/sessions/{id}/stream   the Practice socket (protocol in app/services/practice_session.py)
  - WS     /voice/copilot/{id}/stream    the Live Copilot socket (protocol in app/services/copilot_session.py)
  - GET    /voice/sessions/{id}          session + transcript
  - GET    /voice/sessions/{id}/feedback scores, STAR, delivery, model answers (state "pending" until ready)
  - GET    /voice/history                past sessions
  - GET    /voice/progress               averages per skill area and interview type, weak questions
  - DELETE /voice/sessions/{id}          wipe one session (transcript + feedback)
  - DELETE /voice/history                wipe every voice session
  - GET    /admin/voice/health           admin: sessions, latency p50/p95 per stage, provider errors, spend
"""
from __future__ import annotations

from typing import Literal, Optional

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.voice_schemas import (
    CopilotAckIn, SessionCreateIn, SessionCreateOut, StreamTokenOut, VoiceFeedbackOut, VoiceHealthOut,
    VoiceHistoryOut, VoiceProgressOut, VoiceProvidersOut, VoiceSessionDetail, VoiceSessionOut, VoiceSettingsIn,
    VoiceSettingsOut,
)
from app.services import (
    ai_router, audit, copilot_session, entitlements, practice_session, voice_auth, voice_eval, voice_health,
    voice_speech, voice_store,
)
from app.services.entitlements import require_feature

router = APIRouter(prefix="/voice", tags=["voice"])
admin_router = APIRouter(prefix="/admin/voice", tags=["admin"])

STREAM_PATHS = {"mock": "/api/v1/voice/sessions/{id}/stream", "copilot": "/api/v1/voice/copilot/{id}/stream"}


def _check_mode(db: Session, user, mode: str, *, count: bool) -> None:
    """Practice needs voice_prep; Live Copilot needs live_copilot (kill switch + entitlement) AND the
    current acknowledgement. count=False checks without using a daily-quota slot."""
    feature = "voice_prep" if mode == "mock" else "live_copilot"
    if count:
        entitlements.check_feature(db, user, feature)
    elif not entitlements.effective(db, user.id, feature).enabled:
        raise HTTPException(403, f"Your account does not include {entitlements.FEATURE_LABELS[feature]}.")
    if mode == "copilot":
        row = voice_store.get_voice_settings(db, user.id)
        if row.copilot_ack_version != get_settings().COPILOT_ACK_VERSION:
            raise voice_store.api_error(403, "copilot_ack_required",
                                        "Read and accept the Live Copilot notice before the first session.")


@router.get("/providers", response_model=VoiceProvidersOut)
def providers(user=Depends(require_feature("voice_prep")), db: Session = Depends(get_db)):
    stt, stt_skipped = voice_speech.resolve(db, user_id=user.id, task="stt")
    streaming = bool(stt and stt[0].streaming)
    tts, _ = voice_speech.resolve(db, user_id=user.id, task="tts")
    fast, _ = ai_router.resolve_chain(db, user=user, task="interview_chat")
    strong, _ = ai_router.resolve_chain(db, user=user, task="interview_eval")
    tts_first = tts[0] if tts else None
    tts_out = ({**tts_first.public(), "kind": "browser" if tts_first.provider == "browser" else "server"}
               if tts_first else {"name": "Browser voice", "provider": "browser", "model_id": "speech-synthesis",
                                  "tier": "free", "kind": "browser"})
    # Practice, per-utterance STT: end-of-answer silence + transcription + first sentence + browser voice.
    # With streaming STT the copilot's first words follow the question by about a second.
    expected = (900 if streaming else
                get_settings().VOICE_END_OF_TURN_MS + 400 + 450 + (0 if tts_out["kind"] == "browser" else 400))
    return {
        "stt": stt[0].public() if stt else None, "tts": tts_out,
        "llm_fast": fast[0].display_name if fast else None, "llm_strong": strong[0].display_name if strong else None,
        "streaming_stt": streaming, "diarization": streaming, "expected_first_word_ms": expected,
        "ready": bool(stt and fast),
        "reason": None if stt and fast else (
            "No speech-to-text model is available" + (f" ({stt_skipped[0]})" if stt_skipped else "")
            + " — answers can still be typed." if not stt else "No AI model is available for the interviewer."),
    }


@router.get("/settings", response_model=VoiceSettingsOut)
def get_voice_settings(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return voice_store.settings_out(voice_store.get_voice_settings(db, user.id))


@router.put("/settings", response_model=VoiceSettingsOut)
def put_voice_settings(body: VoiceSettingsIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return voice_store.settings_out(voice_store.update_settings(db, user.id, body.model_dump(exclude_unset=True)))


@router.post("/copilot/acknowledge", response_model=VoiceSettingsOut)
def acknowledge_copilot(body: CopilotAckIn, request: Request, user=Depends(get_current_user),
                        db: Session = Depends(get_db)):
    current = get_settings().COPILOT_ACK_VERSION
    if body.version != current:
        raise voice_store.api_error(409, "ack_version_changed",
                                    "The notice changed — read the new one and accept it.")
    row = voice_store.get_voice_settings(db, user.id)
    if db.get(type(row), user.id) is None:
        db.add(row)
    row.copilot_ack_version = current
    row.copilot_ack_at = datetime.now(timezone.utc).replace(tzinfo=None)
    audit.record(db, actor_id=user.id, action="voice.copilot_acknowledge", target_type="user", target_id=user.id,
                 after={"version": current}, request=request)
    db.commit()
    db.refresh(row)
    return voice_store.settings_out(row)


def _issue(user, session_id: str) -> tuple[str, int]:
    return voice_auth.issue(user.id, session_id, user.session_version or 0), get_settings().VOICE_STREAM_TOKEN_SECONDS


@router.post("/sessions", response_model=SessionCreateOut, status_code=201)
@limiter.limit(lambda: get_settings().RATE_LIMIT_VOICE_SESSIONS)
def create_session(request: Request, body: SessionCreateIn, user=Depends(get_current_user),
                   db: Session = Depends(get_db)):
    _check_mode(db, user, body.mode, count=True)
    live = voice_auth.live_session(user.id)
    if live:
        raise voice_store.api_error(409, "voice_session_busy",
                                    "Another voice session is open. End it first (only one at a time).")
    settings_row = voice_store.get_voice_settings(db, user.id)
    if body.mode == "copilot":
        config = {"answer_style": body.answer_style or settings_row.answer_style or "bullets",
                  "answer_length": body.answer_length or settings_row.answer_length or "short",
                  "sources": body.sources or settings_row.default_sources or "tab_mic",
                  "language": body.language or settings_row.language or "en"}
    else:
        config = {"interview_type": body.interview_type, "difficulty": body.difficulty, "length": body.length,
                  "language": body.language or settings_row.language or "en",
                  "focus_questions": [q.strip()[:300] for q in body.focus_questions if q.strip()]}
    row = voice_store.create_session(db, user.id, mode=body.mode, job_id=body.job_id, role=body.role, config=config)
    token, ttl = _issue(user, row.id)
    return {"session": voice_store.session_out(db, row), "stream_token": token,
            "stream_path": STREAM_PATHS[row.mode].format(id=row.id), "expires_in": ttl}


@router.post("/sessions/{session_id}/token", response_model=StreamTokenOut)
def stream_token(session_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = voice_store.get_owned(db, user.id, session_id)
    _check_mode(db, user, row.mode, count=False)
    if row.state not in voice_store.LIVE_STATES:
        raise voice_store.api_error(409, "session_ended", "This session has ended. Start a new one.")
    token, ttl = _issue(user, row.id)
    return {"stream_token": token, "expires_in": ttl}


@router.websocket("/sessions/{session_id}/stream")
async def practice_stream(websocket: WebSocket, session_id: str):
    await practice_session.run_socket(websocket, session_id)


@router.websocket("/copilot/{session_id}/stream")
async def copilot_stream(websocket: WebSocket, session_id: str):
    await copilot_session.run_socket(websocket, session_id)


@router.get("/sessions/{session_id}", response_model=VoiceSessionDetail)
def get_session(session_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = voice_store.get_owned(db, user.id, session_id)
    return {"session": voice_store.session_out(db, row),
            "turns": [voice_store.turn_out(t) for t in voice_store.turns(db, row.id)]}


@router.get("/sessions/{session_id}/feedback", response_model=VoiceFeedbackOut)
def get_feedback(session_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return voice_eval.feedback_out(db, voice_store.get_owned(db, user.id, session_id))


@router.get("/history", response_model=VoiceHistoryOut)
def history(mode: Optional[Literal["mock", "copilot"]] = None, limit: int = Query(20, ge=1, le=100),
            offset: int = Query(0, ge=0), user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows, total = voice_store.history(db, user.id, mode=mode, limit=limit, offset=offset)
    return {"sessions": [voice_store.session_out(db, r) for r in rows], "total": total}


@router.get("/progress", response_model=VoiceProgressOut)
def progress(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return voice_store.progress(db, user.id)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = voice_store.get_owned(db, user.id, session_id)
    if voice_auth.live_session(user.id) == row.id:
        raise voice_store.api_error(409, "voice_session_live", "End the session before deleting it.")
    voice_store.delete_session(db, row)


@router.delete("/history", status_code=200)
def delete_history(user=Depends(get_current_user), db: Session = Depends(get_db)):
    if voice_auth.live_session(user.id):
        raise voice_store.api_error(409, "voice_session_live", "End the open session before deleting your history.")
    return {"deleted": voice_store.delete_all(db, user.id)}


@admin_router.get("/health", response_model=VoiceHealthOut)
def voice_health_view(days: int = Query(7, ge=1, le=90), actor=Depends(require_role("admin")),
                      db: Session = Depends(get_db)):
    return voice_health.summary(db, since=datetime.now(timezone.utc) - timedelta(days=days), days=days)


__all__ = ["router", "admin_router", "VoiceSessionOut"]
