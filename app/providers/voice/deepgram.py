"""
app/providers/voice/deepgram.py
---------------------------------
Deepgram streaming speech-to-text (P7b, Live Copilot). One WebSocket per
audio channel to wss://api.deepgram.com/v1/listen with 16 kHz linear16 PCM:
interim results (partials), endpointing + UtteranceEnd (end of a turn),
optional diarization for the mic-only "mixed" channel. `[verify model name
and price before use]` — the registry row says nova-3.

Audio goes to Deepgram and nowhere else; nothing is kept here. The
connection is re-opened once if it drops; KeepAlive is sent during silence
so Deepgram doesn't close an idle stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from urllib.parse import urlencode

from app.providers.voice.base import BYTES_PER_MS, SpeechError, Transcript

logger = logging.getLogger("job_finder_api.deepgram")

LISTEN_URL = "wss://api.deepgram.com/v1/listen"
KEEPALIVE_SECONDS = 5


def listen_url(*, model_id: str, language: str | None, diarize: bool, endpointing_ms: int,
               utterance_end_ms: int) -> str:
    params = {"model": model_id, "encoding": "linear16", "sample_rate": 16000, "channels": 1,
              "interim_results": "true", "smart_format": "true", "punctuate": "true",
              "endpointing": endpointing_ms, "utterance_end_ms": utterance_end_ms, "vad_events": "true"}
    if language:
        params["language"] = "multi" if language in ("multi", "hi-en") else language
    if diarize:
        params["diarize"] = "true"
    return f"{LISTEN_URL}?{urlencode(params)}"


def parse_message(raw: str | bytes) -> Transcript | None:
    """One Deepgram message -> Transcript (partial / final / utterance end), or None."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    kind = data.get("type")
    if kind == "UtteranceEnd":
        end = int(float(data.get("last_word_end") or 0) * 1000)
        return Transcript(text="", final=True, start_ms=end, end_ms=end, utterance_end=True)
    if kind != "Results":
        return None
    alts = ((data.get("channel") or {}).get("alternatives") or [{}])
    alt = alts[0] if alts else {}
    text = (alt.get("transcript") or "").strip()
    start = float(data.get("start") or 0)
    duration = float(data.get("duration") or 0)
    words = [{"word": w.get("punctuated_word") or w.get("word"), "start_ms": int(float(w.get("start", 0)) * 1000),
              "end_ms": int(float(w.get("end", 0)) * 1000), "speaker": w.get("speaker")}
             for w in alt.get("words") or []]
    speakers = Counter(w["speaker"] for w in words if w["speaker"] is not None)
    final = bool(data.get("is_final"))
    return Transcript(text=text, final=final, start_ms=int(start * 1000), end_ms=int((start + duration) * 1000),
                      words=words, speaker=speakers.most_common(1)[0][0] if speakers else None,
                      utterance_end=final and bool(data.get("speech_final")))


class DeepgramStreamingSTT:
    def __init__(self, *, model_id: str, api_key: str, language: str | None = None, diarize: bool = False,
                 endpointing_ms: int = 300, utterance_end_ms: int = 1000, connect=None):
        if not api_key:
            raise SpeechError("No deepgram key is set up.", retryable=False)
        self.url = listen_url(model_id=model_id, language=language, diarize=diarize,
                              endpointing_ms=endpointing_ms, utterance_end_ms=utterance_end_ms)
        self.api_key = api_key
        self._connect = connect
        self.events: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._bytes = 0
        self._last_send = 0.0
        self._reconnected = False
        self._closing = False

    def _connector(self):
        if self._connect is not None:
            return self._connect
        from websockets.asyncio.client import connect
        return connect

    async def _open(self) -> None:
        try:
            self._ws = await self._connector()(self.url, additional_headers={"Authorization": f"Token {self.api_key}"},
                                               open_timeout=5, max_size=2 ** 20)
        except Exception as e:  # noqa: BLE001 — any connect failure means "this provider can't run now"
            raise SpeechError(f"Deepgram connection failed: {type(e).__name__}") from e
        self._reader = asyncio.create_task(self._read(self._ws))

    async def start(self) -> None:
        await self._open()
        self._last_send = asyncio.get_running_loop().time()
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                t = parse_message(raw)
                if t is not None and (t.text or t.utterance_end):
                    self.events.put_nowait(t)
        except Exception as e:  # noqa: BLE001 — a dropped stream is re-opened by the next send()
            logger.warning("deepgram: stream read ended: %s", type(e).__name__)

    async def _keep_alive(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing:
            await asyncio.sleep(1)
            if self._ws is not None and loop.time() - self._last_send >= KEEPALIVE_SECONDS:
                try:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
                    self._last_send = loop.time()
                except Exception:  # noqa: BLE001 — send() reconnects on the next audio frame
                    pass

    async def send(self, pcm: bytes) -> None:
        if self._closing:
            return
        try:
            await self._ws.send(pcm)
        except Exception:  # noqa: BLE001
            if self._reconnected:
                raise SpeechError("Deepgram stream dropped twice", retryable=True)
            self._reconnected = True
            await self._open()  # one reconnect per session (P7 plan 2.3)
            await self._ws.send(pcm)
        self._bytes += len(pcm)
        self._last_send = asyncio.get_running_loop().time()

    async def close(self) -> float:
        self._closing = True
        if self._keepalive:
            self._keepalive.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                if self._reader:
                    await asyncio.wait_for(asyncio.shield(self._reader), timeout=2)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader and not self._reader.done():
            self._reader.cancel()
        self.events.put_nowait(None)
        return self._bytes / BYTES_PER_MS / 1000
