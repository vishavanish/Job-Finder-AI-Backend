"""
app/services/voice_speech.py
------------------------------
Picks the speech-to-text and text-to-speech models for a voice session
through the registry (same order and rules as ai_router: user's choice ->
admin default -> priority; paid / Free tools only / missing key / breaker
skip a model) and calls them with failover.

Usage is ledgered per call: tokens_in = audio seconds (STT) or characters
(TTS), so a speech row's cost_per_1k_in is the price per 1,000 of those.
Keys are resolved once when the session starts, kept in memory, never sent
to the client or logged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ai import AiModel
from app.models.user import User
from app.providers.voice import adapters
from app.providers.voice.base import SpeechError, Transcript, duration_ms
from app.services import ai_router

logger = logging.getLogger("job_finder_api.voice_speech")


@dataclass
class SpeechChoice:
    model_ref: str
    provider: str
    model_id: str
    display_name: str
    tier: str
    base_url: str | None
    api_key: str
    key_source: str

    def public(self) -> dict:
        return {"name": self.display_name, "provider": self.provider, "model_id": self.model_id, "tier": self.tier}


def resolve(db: Session, *, user_id: str, task: str, free_only: bool = False) -> tuple[list[SpeechChoice], list[str]]:
    user = db.get(User, user_id)
    chain, skipped = ai_router.resolve_chain(db, user=user, task=task, free_only=free_only)
    out = []
    for m in chain:
        key, source = ai_router.resolve_key(db, user_id=user_id, model=m)
        out.append(SpeechChoice(m.id, m.provider, m.model_id, m.display_name, m.tier, m.base_url, key, source))
    return out, skipped


def make_stt(choice: SpeechChoice):
    """Adapter for one STT choice (tests replace this)."""
    return adapters.build_stt(choice.provider, model_id=choice.model_id, base_url=choice.base_url,
                              api_key=choice.api_key, timeout=float(get_settings().VOICE_STT_TIMEOUT_SECONDS))


def make_tts(choice: SpeechChoice):
    return adapters.build_tts(choice.provider, model_id=choice.model_id, base_url=choice.base_url,
                              api_key=choice.api_key, timeout=float(get_settings().VOICE_STT_TIMEOUT_SECONDS))


def _ledger(user_id: str, task: str, choice: SpeechChoice, *, units: int, latency_ms: int, success: bool,
            error: str | None, ref: str) -> float:
    db = SessionLocal()
    try:
        model = db.get(AiModel, choice.model_ref)
        if model is None:
            return 0.0
        return ai_router.record_usage(db, user_id=user_id, task=task, model=model, key_source=choice.key_source,
                                      tokens_in=units, tokens_out=0, latency_ms=latency_ms, success=success,
                                      error=error, ref=ref)
    finally:
        db.close()


class SpeechRouter:
    """STT + TTS for one session. Failover moves down the chain; a model
    that fails is skipped for the rest of the session."""

    def __init__(self, *, user_id: str, ref: str, stt: list[SpeechChoice], tts: list[SpeechChoice]):
        self.user_id = user_id
        self.ref = ref
        self.stt = list(stt)
        self.tts = list(tts)

    @property
    def tts_kind(self) -> str:
        return "browser" if not self.tts or self.tts[0].provider == "browser" else "server"

    def describe(self) -> dict:
        return {"stt": self.stt[0].public() if self.stt else None,
                "tts": {**self.tts[0].public(), "kind": self.tts_kind} if self.tts else
                {"name": "Browser voice", "provider": "browser", "kind": "browser", "tier": "free"}}

    async def transcribe(self, pcm: bytes, *, language: str | None = None) -> tuple[Transcript, float]:
        """(transcript, cost). Raises SpeechError when every model failed."""
        errors: list[str] = []
        while self.stt:
            choice = self.stt[0]
            started = time.monotonic()
            seconds = max(1, round(duration_ms(pcm) / 1000))
            try:
                result = await make_stt(choice).transcribe(pcm, language=language)
            except SpeechError as e:
                latency = int((time.monotonic() - started) * 1000)
                await asyncio.to_thread(_ledger, self.user_id, "stt", choice, units=seconds, latency_ms=latency,
                                        success=False, error=str(e), ref=self.ref)
                errors.append(f"{choice.display_name}: {e}")
                logger.warning("voice: stt %s failed session=%s: %s", choice.model_id, self.ref, e)
                if e.retryable and len(self.stt) == 1:
                    raise
                self.stt.pop(0)
                continue
            latency = int((time.monotonic() - started) * 1000)
            cost = await asyncio.to_thread(_ledger, self.user_id, "stt", choice, units=seconds, latency_ms=latency,
                                           success=True, error=None, ref=self.ref)
            return result, cost
        raise SpeechError("; ".join(errors) or "no speech-to-text model is available", retryable=False)

    async def speak(self, text: str, *, voice: str | None = None) -> tuple[bytes, float]:
        """(audio, cost); empty audio when the browser speaks. Falls back to
        the browser voice when every server voice failed."""
        while self.tts and self.tts[0].provider != "browser":
            choice = self.tts[0]
            started = time.monotonic()
            try:
                audio = await make_tts(choice).speak(text, voice=voice)
            except SpeechError as e:
                latency = int((time.monotonic() - started) * 1000)
                await asyncio.to_thread(_ledger, self.user_id, "tts", choice, units=len(text), latency_ms=latency,
                                        success=False, error=str(e), ref=self.ref)
                logger.warning("voice: tts %s failed session=%s: %s", choice.model_id, self.ref, e)
                self.tts.pop(0)
                continue
            latency = int((time.monotonic() - started) * 1000)
            cost = await asyncio.to_thread(_ledger, self.user_id, "tts", choice, units=len(text),
                                           latency_ms=latency, success=True, error=None, ref=self.ref)
            return audio, cost
        return b"", 0.0
