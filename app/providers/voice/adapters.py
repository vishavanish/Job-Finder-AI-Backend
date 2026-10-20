"""
app/providers/voice/adapters.py
---------------------------------
Speech adapters, one per registry provider (CLAUDE.md 7.9):

  stt  groq               Groq Whisper (OpenAI-compatible /audio/transcriptions)
       openai_compatible  any /audio/transcriptions endpoint (OpenAI, ...)
  tts  browser            nothing to call: the client's browser speaks the text
       openai_compatible  any /audio/speech endpoint (OpenAI, ...) -> MP3 bytes

The model and key come from the router (app/services/voice_speech.py);
nothing here reads settings or env keys. Audio stays in memory.
"""
from __future__ import annotations

import httpx

from app.providers.voice.base import SpeechError, Transcript, duration_ms, pcm_to_wav

GROQ_OPENAI_BASE = "https://api.groq.com/openai/v1"


def _short(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}".replace("\n", " ")[:280]


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        retryable = resp.status_code == 429 or resp.status_code >= 500
        raise SpeechError(f"HTTP {resp.status_code} from speech endpoint", retryable=retryable)


class OpenAICompatibleSTT:
    """POST {base_url}/audio/transcriptions with the utterance as a WAV file."""

    def __init__(self, *, base_url: str, model_id: str, api_key: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        self.timeout = timeout

    async def transcribe(self, pcm: bytes, *, language: str | None = None) -> Transcript:
        data = {"model": self.model_id, "response_format": "json", "temperature": "0"}
        if language:
            data["language"] = language.split("-")[0]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                resp = await client.post(f"{self.base_url}/audio/transcriptions", headers=headers, data=data,
                                         files={"file": ("utterance.wav", pcm_to_wav(pcm), "audio/wav")})
        except httpx.HTTPError as e:
            raise SpeechError(_short(e)) from e
        _raise_for(resp)
        try:
            text = (resp.json().get("text") or "").strip()
        except ValueError as e:
            raise SpeechError("unexpected response shape from speech endpoint") from e
        return Transcript(text=text, final=True, start_ms=0, end_ms=duration_ms(pcm))


class BrowserTTS:
    """The client speaks the text with speechSynthesis; the server sends none."""
    kind = "browser"

    async def speak(self, text: str, *, voice: str | None = None) -> bytes:
        return b""


class OpenAICompatibleTTS:
    kind = "server"

    def __init__(self, *, base_url: str, model_id: str, api_key: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        self.timeout = timeout

    async def speak(self, text: str, *, voice: str | None = None) -> bytes:
        body = {"model": self.model_id, "input": text, "voice": voice or "alloy", "response_format": "mp3"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                resp = await client.post(f"{self.base_url}/audio/speech", headers=headers, json=body)
        except httpx.HTTPError as e:
            raise SpeechError(_short(e)) from e
        _raise_for(resp)
        return resp.content


def build_stt(provider: str, *, model_id: str, base_url: str | None, api_key: str, timeout: float):
    if provider == "groq":
        return OpenAICompatibleSTT(base_url=GROQ_OPENAI_BASE, model_id=model_id, api_key=api_key, timeout=timeout)
    if provider == "openai_compatible" and base_url:
        return OpenAICompatibleSTT(base_url=base_url, model_id=model_id, api_key=api_key, timeout=timeout)
    raise SpeechError(f"no speech-to-text adapter for provider {provider!r}", retryable=False)


def build_tts(provider: str, *, model_id: str, base_url: str | None, api_key: str, timeout: float):
    if provider == "browser":
        return BrowserTTS()
    if provider == "openai_compatible" and base_url:
        return OpenAICompatibleTTS(base_url=base_url, model_id=model_id, api_key=api_key, timeout=timeout)
    raise SpeechError(f"no text-to-speech adapter for provider {provider!r}", retryable=False)
