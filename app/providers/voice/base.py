"""
app/providers/voice/base.py
-----------------------------
Speech interfaces (CLAUDE.md 7.9 / 7.10). Audio is 16 kHz mono 16-bit PCM
held in memory only: nothing here writes audio to disk, DB or logs.

P7a speech-to-text is per utterance (the server's VAD cuts the stream into
utterances, each is sent once); streaming STT (Deepgram) arrives with the
copilot in P7b behind the same router task.
"""
from __future__ import annotations

import asyncio
import io
import wave
from dataclasses import dataclass, field
from typing import Protocol

SAMPLE_RATE = 16000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 32 bytes of PCM16 per millisecond


class SpeechError(Exception):
    """A speech call failed. `retryable` = worth another try (429, 5xx, timeout)."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Transcript:
    text: str
    final: bool = True
    start_ms: int = 0
    end_ms: int = 0
    words: list[dict] = field(default_factory=list)  # [{word, start_ms, end_ms}] when the provider gives them
    speaker: int | None = None     # diarization: the main speaker of this piece
    utterance_end: bool = False    # the speaker stopped (endpointing / UtteranceEnd)


class STTProvider(Protocol):
    async def transcribe(self, pcm: bytes, *, language: str | None = None,
                         prompt: str | None = None) -> Transcript: ...


class StreamingSTTProvider(Protocol):
    """True streaming STT (P7b): audio in as it arrives, partial and final
    transcripts out on `events`, `None` on the queue when the stream ends."""
    events: "asyncio.Queue[Transcript | None]"

    async def start(self) -> None: ...

    async def send(self, pcm: bytes) -> None: ...

    async def close(self) -> float: ...  # seconds of audio sent (for the usage ledger)


class TTSProvider(Protocol):
    kind: str  # "browser" (the client speaks the text) | "server" (audio bytes are sent)

    async def speak(self, text: str, *, voice: str | None = None) -> bytes: ...


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wraps PCM16 mono 16 kHz in a WAV header, in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def duration_ms(pcm: bytes) -> int:
    return len(pcm) // BYTES_PER_MS
