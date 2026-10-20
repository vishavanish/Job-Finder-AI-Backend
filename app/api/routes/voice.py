"""
app/api/routes/voice.py
-------------------------
Voice interview prep (CLAUDE.md 7.10, P7a Practice mode):

  - GET    /voice/providers              which speech / AI models a session would use, expected latency
  - GET    /voice/settings               voice choices + the Live Copilot acknowledgement text and state
  - PUT    /voice/settings
  - POST   /voice/sessions               start a Practice session -> {session, stream_token, stream_path}
  - POST   /voice/sessions/{id}/token    a fresh single-use stream token (reconnect)
  - WS     /voice/sessions/{id}/stream   the Practice socket (protocol in app/services/practice_session.py)
  - GET    /voice/sessions/{id}          session + transcript
  - GET    /voice/sessions/{id}/feedback scores, STAR, delivery, model answers (state "pending" until ready)
  - GET    /voice/history                past sessions
  - GET    /voice/progress               averages per skill area and interview type, weak questions
  - DELETE /voice/sessions/{id}          wipe one session (transcript + feedback)
  - DELETE /voice/history                wipe every voice session

Live Copilot (WS /voice/copilot/{id}/stream, acknowledgement) is P7b.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.voice_schemas import (
    SessionCreateIn, SessionCreateOut, StreamTokenOut, VoiceFeedbackOut, VoiceHistoryOut, VoiceProgressOut,
    VoiceProvidersOut, VoiceSessionDetail, VoiceSessionOut, VoiceSettingsIn, VoiceSettingsOut,
)
from app.services import ai_router, practice_session, voice_auth, voice_eval, voice_speech, voice_store
from app.services.entitlements import require_feature

router = APIRouter(prefix="/voice", tags=["voice"])

STREAM_PATH = "/api/v1/voice/sessions/{id}/stream"


@router.get("/providers", response_model=VoiceProvidersOut)
def providers(user=Depends(require_feature("voice_prep")), db: Session = Depends(get_db)):
    stt, stt_skipped = voice_speech.resolve(db, user_id=user.id, task="stt")
    tts, _ = voice_speech.resolve(db, user_id=user.id, task="tts")
    fast, _ = ai_router.resolve_chain(db, user=user, task="interview_chat")
    strong, _ = ai_router.resolve_chain(db, user=user, task="interview_eval")
    tts_first = tts[0] if tts else None
    tts_out = ({**tts_first.public(), "kind": "browser" if tts_first.provider == "browser" else "server"}
               if tts_first else {"name": "Browser voice", "provider": "browser", "model_id": "speech-synthesis",
                                  "tier": "free", "kind": "browser"})
    # Per-utterance STT: end-of-answer silence + transcription + first sentence + browser voice.
    expected = get_settings().VOICE_END_OF_TURN_MS + 400 + 450 + (0 if tts_out["kind"] == "browser" else 400)
    return {
        "stt": stt[0].public() if stt else None, "tts": tts_out,
        "llm_fast": fast[0].display_name if fast else None, "llm_strong": strong[0].display_name if strong else None,
        "streaming_stt": False, "diarization": False, "expected_first_word_ms": expected,
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


def _issue(user, session_id: str) -> tuple[str, int]:
    return voice_auth.issue(user.id, session_id, user.session_version or 0), get_settings().VOICE_STREAM_TOKEN_SECONDS


@router.post("/sessions", response_model=SessionCreateOut, status_code=201)
@limiter.limit(lambda: get_settings().RATE_LIMIT_VOICE_SESSIONS)
def create_session(request: Request, body: SessionCreateIn, user=Depends(require_feature("voice_prep")),
                   db: Session = Depends(get_db)):
    live = voice_auth.live_session(user.id)
    if live:
        raise voice_store.api_error(409, "voice_session_busy",
                                    "Another voice session is open. End it first (only one at a time).")
    settings_row = voice_store.get_voice_settings(db, user.id)
    config = {"interview_type": body.interview_type, "difficulty": body.difficulty, "length": body.length,
              "language": body.language or settings_row.language or "en",
              "focus_questions": [q.strip()[:300] for q in body.focus_questions if q.strip()]}
    row = voice_store.create_session(db, user.id, mode=body.mode, job_id=body.job_id, role=body.role, config=config)
    token, ttl = _issue(user, row.id)
    return {"session": voice_store.session_out(db, row), "stream_token": token,
            "stream_path": STREAM_PATH.format(id=row.id), "expires_in": ttl}


@router.post("/sessions/{session_id}/token", response_model=StreamTokenOut)
def stream_token(session_id: str, user=Depends(require_feature("voice_prep")), db: Session = Depends(get_db)):
    row = voice_store.get_owned(db, user.id, session_id)
    if row.state not in voice_store.LIVE_STATES:
        raise voice_store.api_error(409, "session_ended", "This session has ended. Start a new one.")
    token, ttl = _issue(user, row.id)
    return {"stream_token": token, "expires_in": ttl}


@router.websocket("/sessions/{session_id}/stream")
async def practice_stream(websocket: WebSocket, session_id: str):
    await practice_session.run_socket(websocket, session_id)


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


__all__ = ["router", "VoiceSessionOut"]
