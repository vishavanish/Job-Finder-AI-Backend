"""
app/providers/voice/vad.py
----------------------------
Voice activity detection: cuts a stream of PCM16 frames into utterances.

webrtcvad (C, tiny) decides speech per 20 ms; when it isn't installed a
simple energy threshold is used instead. An utterance ends after
`end_silence_ms` of silence; sounds shorter than `min_speech_ms` are
ignored; very long speech is cut into pieces of `max_utterance_ms` so a
long answer is transcribed while the user is still talking.
"""
from __future__ import annotations

import array
import math
from collections import deque
from dataclasses import dataclass

from app.providers.voice.base import BYTES_PER_MS

CHUNK_MS = 20
CHUNK_BYTES = CHUNK_MS * BYTES_PER_MS
PREROLL_MS = 200          # audio kept from just before speech starts
START_CHUNKS = 3          # voiced 20 ms chunks in a row before speech counts as started
ENERGY_THRESHOLD = 500.0  # RMS on PCM16, used only without webrtcvad


@dataclass
class SpeechStart:
    at_ms: int


@dataclass
class Utterance:
    pcm: bytes
    start_ms: int
    end_ms: int
    final: bool = True  # False = a piece of a still-running long utterance


def _energy_is_speech(chunk: bytes) -> bool:
    samples = array.array("h", chunk)
    if not samples:
        return False
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return rms >= ENERGY_THRESHOLD


class Segmenter:
    def __init__(self, *, end_silence_ms: int = 700, min_speech_ms: int = 300, max_utterance_ms: int = 60000,
                 aggressiveness: int = 2, use_webrtc: bool = True):
        self.end_silence_ms = end_silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_ms = max_utterance_ms
        self._vad = None
        if use_webrtc:
            try:
                import webrtcvad
                self._vad = webrtcvad.Vad(max(0, min(3, aggressiveness)))
            except ImportError:
                self._vad = None
        self._rest = b""
        self._clock_ms: int | None = None
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_MS // CHUNK_MS)
        self._voiced_run = 0
        self.in_speech = False
        self._buf = bytearray()
        self._start_ms = 0
        self._speech_ms = 0
        self._silence_ms = 0

    @property
    def engine(self) -> str:
        return "webrtcvad" if self._vad is not None else "energy"

    def _is_speech(self, chunk: bytes) -> bool:
        if self._vad is not None:
            return self._vad.is_speech(chunk, 16000)
        return _energy_is_speech(chunk)

    def feed(self, pcm: bytes, ts_ms: int | None = None) -> list[SpeechStart | Utterance]:
        """Feeds one frame (any length, PCM16). ts_ms = the client's time of
        the frame's first sample; frames without one follow on."""
        if ts_ms is not None and (self._clock_ms is None or not self._rest):
            self._clock_ms = ts_ms - len(self._rest) // BYTES_PER_MS
        if self._clock_ms is None:
            self._clock_ms = 0
        data = self._rest + pcm
        events: list[SpeechStart | Utterance] = []
        offset = 0
        while offset + CHUNK_BYTES <= len(data):
            chunk = data[offset:offset + CHUNK_BYTES]
            offset += CHUNK_BYTES
            events.extend(self._step(chunk, self._clock_ms))
            self._clock_ms += CHUNK_MS
        self._rest = data[offset:]
        return events

    def _step(self, chunk: bytes, at_ms: int) -> list[SpeechStart | Utterance]:
        voiced = self._is_speech(chunk)
        if not self.in_speech:
            self._preroll.append(chunk)
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            if self._voiced_run < START_CHUNKS:
                return []
            self.in_speech = True
            self._buf = bytearray(b"".join(self._preroll))
            self._start_ms = at_ms + CHUNK_MS - len(self._buf) // BYTES_PER_MS
            self._speech_ms = self._voiced_run * CHUNK_MS
            self._silence_ms = 0
            self._preroll.clear()
            return [SpeechStart(self._start_ms)]

        self._buf.extend(chunk)
        if voiced:
            self._speech_ms += CHUNK_MS
            self._silence_ms = 0
        else:
            self._silence_ms += CHUNK_MS
        end_ms = at_ms + CHUNK_MS
        if self._silence_ms >= self.end_silence_ms:
            return self._close(end_ms - self._silence_ms, final=True)
        if end_ms - self._start_ms >= self.max_utterance_ms:
            return self._close(end_ms, final=False)
        return []

    def _close(self, end_ms: int, *, final: bool) -> list[SpeechStart | Utterance]:
        keep = (end_ms - self._start_ms) * BYTES_PER_MS
        pcm = bytes(self._buf[:keep])
        enough = self._speech_ms >= self.min_speech_ms
        start = self._start_ms
        if final:
            self.in_speech = False
            self._voiced_run = 0
            self._buf = bytearray()
        else:
            self._buf = bytearray()
            self._start_ms = end_ms
        self._speech_ms = 0
        self._silence_ms = 0
        return [Utterance(pcm, start, end_ms, final)] if enough else []

    def flush(self) -> list[Utterance]:
        """Ends any speech in progress (pause, stop, typed answer)."""
        if not self.in_speech:
            return []
        end = self._start_ms + len(self._buf) // BYTES_PER_MS
        return [e for e in self._close(end, final=True) if isinstance(e, Utterance)]
