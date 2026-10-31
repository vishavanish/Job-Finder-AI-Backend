"""
app/providers/llm/adapters.py
-------------------------------
One adapter per provider type an admin can choose in the model registry:

  gemini            Google Gemini (google-genai SDK)
  groq              Groq (groq SDK)
  huggingface       Hugging Face Inference (huggingface_hub)
  anthropic         Anthropic Claude (anthropic SDK)
  openai_compatible Any /chat/completions endpoint: OpenAI, OpenRouter,
                    Together, DeepSeek, Mistral, a local Ollama, ...
                    (needs the model's base_url)

Each call gets the model and key from the router; nothing here reads
settings or env keys. SDK imports are lazy so a missing optional package
only breaks its own provider.

stream() (P7, voice) yields the answer in chunks and fills `usage` with
{"tokens_in", "tokens_out"} when the provider reports them. Adapters
without it are streamed as one chunk by stream_text().
"""
from __future__ import annotations

import base64
import json
import re
from typing import Iterator

import httpx

from app.providers.llm.base import LLMError, LLMResult, ModelSpec, short_error

TEMPERATURE = 0.1

_GEMINI_VERSION = re.compile(r"gemini-(\d+)(?:\.(\d+))?")


def gemini_thinking(spec: ModelSpec) -> dict | None:
    """The least thinking a Gemini model allows, for fast tasks. 2.5 Flash
    turns it off with a zero budget (Pro can't); Gemini 3+ takes a level
    ("minimal" on Flash, "low" on Pro). Older models don't think."""
    if not spec.fast:
        return None
    m = _GEMINI_VERSION.match(spec.model_id.lower().removeprefix("models/"))
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    pro = "pro" in spec.model_id.lower()
    if major >= 3:
        return {"thinking_level": "low" if pro else "minimal"}
    if (major, minor) >= (2, 5) and not pro:
        return {"thinking_budget": 0}
    return None


def groq_reasoning(spec: ModelSpec) -> dict:
    """GPT-OSS and Qwen on Groq reason before they answer; fast tasks ask for the least. Qwen's thoughts are
    otherwise mixed into the reply as <think> text, so they are always hidden."""
    model = spec.model_id.lower()
    if "gpt-oss" in model:
        return {"reasoning_effort": "low", "include_reasoning": False} if spec.fast else {}
    if "qwen" in model:
        return {"reasoning_effort": "none"} if spec.fast else {"reasoning_format": "hidden"}
    return {}


def _rejects_thinking(e: Exception) -> bool:
    text = str(e).lower()
    return "thinking" in text or "reasoning" in text


class GeminiAdapter:
    def complete(self, spec: ModelSpec, *, api_key: str, system: str, user: str,
                 json_mode: bool, max_tokens: int, timeout: float) -> LLMResult:
        try:
            from google import genai
        except ImportError as e:
            raise LLMError("google-genai is not installed", retryable=False) from e
        config: dict = {"system_instruction": system, "temperature": TEMPERATURE,
                        "max_output_tokens": max_tokens}
        if json_mode:
            config["response_mime_type"] = "application/json"
        thinking = gemini_thinking(spec)
        if thinking:
            config["thinking_config"] = thinking
        try:
            client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout * 1000)})
            try:
                resp = client.models.generate_content(model=spec.model_id, contents=user, config=config)
            except Exception as e:  # noqa: BLE001
                if not (thinking and _rejects_thinking(e)):
                    raise
                config.pop("thinking_config")  # this model won't take that setting: ask again without it
                resp = client.models.generate_content(model=spec.model_id, contents=user, config=config)
        except Exception as e:  # noqa: BLE001 — SDK raises many types; the router only needs "failed"
            raise LLMError(short_error(e)) from e
        usage = getattr(resp, "usage_metadata", None)
        return LLMResult(
            text=resp.text or "",
            tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
            tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
        )


    def stream(self, spec: ModelSpec, *, api_key: str, system: str, user: str, max_tokens: int,
               timeout: float, usage: dict, images: list[tuple[str, bytes]] | None = None) -> Iterator[str]:
        try:
            from google import genai
        except ImportError as e:
            raise LLMError("google-genai is not installed", retryable=False) from e
        config = {"system_instruction": system, "temperature": TEMPERATURE, "max_output_tokens": max_tokens}
        thinking = gemini_thinking(spec)
        if thinking:
            config["thinking_config"] = thinking
        contents: object = user
        if images:
            from google.genai import types
            contents = [*(types.Part.from_bytes(data=data, mime_type=mime) for mime, data in images), user]
        try:
            client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout * 1000)})
            sent = False
            while True:
                try:
                    for chunk in client.models.generate_content_stream(model=spec.model_id, contents=contents,
                                                                       config=config):
                        meta = getattr(chunk, "usage_metadata", None)
                        if meta is not None:
                            usage["tokens_in"] = int(getattr(meta, "prompt_token_count", 0) or 0)
                            usage["tokens_out"] = int(getattr(meta, "candidates_token_count", 0) or 0)
                        if chunk.text:
                            sent = True
                            yield chunk.text
                    break
                except Exception as e:  # noqa: BLE001
                    if sent or "thinking_config" not in config or not _rejects_thinking(e):
                        raise
                    config.pop("thinking_config")  # this model won't take that setting: ask again without it
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(short_error(e)) from e


def _openai_style_result(resp) -> LLMResult:
    usage = getattr(resp, "usage", None)
    return LLMResult(
        text=(resp.choices[0].message.content or "").strip(),
        tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
        tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
    )


class GroqAdapter:
    def complete(self, spec: ModelSpec, *, api_key: str, system: str, user: str,
                 json_mode: bool, max_tokens: int, timeout: float) -> LLMResult:
        try:
            from groq import Groq
        except ImportError as e:
            raise LLMError("groq is not installed", retryable=False) from e
        try:
            client = Groq(api_key=api_key, timeout=timeout, max_retries=1)
            resp = client.chat.completions.create(
                model=spec.model_id,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=TEMPERATURE,
                max_tokens=max_tokens,
                **groq_reasoning(spec),
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(short_error(e)) from e
        return _openai_style_result(resp)

    def stream(self, spec: ModelSpec, *, api_key: str, system: str, user: str, max_tokens: int,
               timeout: float, usage: dict, images: list[tuple[str, bytes]] | None = None) -> Iterator[str]:
        if images:
            raise LLMError("this model does not read images", retryable=False)
        try:
            from groq import Groq
        except ImportError as e:
            raise LLMError("groq is not installed", retryable=False) from e
        try:
            client = Groq(api_key=api_key, timeout=timeout, max_retries=0)
            resp = client.chat.completions.create(
                model=spec.model_id,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=TEMPERATURE,
                max_tokens=max_tokens,
                stream=True,
                **groq_reasoning(spec),
            )
            for chunk in resp:
                extra = getattr(chunk, "x_groq", None)
                u = getattr(extra, "usage", None) if extra is not None else None
                if u is not None:
                    usage["tokens_in"] = int(getattr(u, "prompt_tokens", 0) or 0)
                    usage["tokens_out"] = int(getattr(u, "completion_tokens", 0) or 0)
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(short_error(e)) from e


class HuggingFaceAdapter:
    def complete(self, spec: ModelSpec, *, api_key: str, system: str, user: str,
                 json_mode: bool, max_tokens: int, timeout: float) -> LLMResult:
        try:
            from huggingface_hub import InferenceClient
        except ImportError as e:
            raise LLMError("huggingface_hub is not installed", retryable=False) from e
        try:
            client = InferenceClient(api_key=api_key, timeout=timeout)
            resp = client.chat_completion(
                model=spec.model_id,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=TEMPERATURE,
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(short_error(e)) from e
        return _openai_style_result(resp)


class AnthropicAdapter:
    """Claude via the official SDK. Sends only model, max_tokens, system and
    messages: sampling parameters and explicit thinking settings are
    rejected by current Claude models, and the admin may pick any of them.
    A refusal counts as a failed call, so the router moves on to the next
    model in the chain."""

    def complete(self, spec: ModelSpec, *, api_key: str, system: str, user: str,
                 json_mode: bool, max_tokens: int, timeout: float) -> LLMResult:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("anthropic is not installed", retryable=False) from e
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
        try:
            resp = client.messages.create(
                model=spec.model_id,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, anthropic.BadRequestError) as e:
            raise LLMError(short_error(e), retryable=False) from e
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(short_error(e)) from e

        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request (refusal)", retryable=False)
        text = "".join(block.text for block in resp.content if block.type == "text")
        return LLMResult(text=text, tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens)

    def stream(self, spec: ModelSpec, *, api_key: str, system: str, user: str, max_tokens: int,
               timeout: float, usage: dict, images: list[tuple[str, bytes]] | None = None) -> Iterator[str]:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("anthropic is not installed", retryable=False) from e
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
        content: object = user
        if images:
            content = [*({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                       "data": base64.b64encode(data).decode()}}
                         for mime, data in images), {"type": "text", "text": user}]
        try:
            with client.messages.stream(model=spec.model_id, max_tokens=max_tokens, system=system,
                                        messages=[{"role": "user", "content": content}]) as s:
                for text in s.text_stream:
                    yield text
                final = s.get_final_message()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, anthropic.BadRequestError) as e:
            raise LLMError(short_error(e), retryable=False) from e
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(short_error(e)) from e
        usage["tokens_in"] = final.usage.input_tokens
        usage["tokens_out"] = final.usage.output_tokens
        if final.stop_reason == "refusal":
            raise LLMError("model declined the request (refusal)", retryable=False)


class OpenAICompatibleAdapter:
    """POST {base_url}/chat/completions. The key is optional (a local Ollama
    needs none). base_url is set by an admin, never by a user."""

    def complete(self, spec: ModelSpec, *, api_key: str, system: str, user: str,
                 json_mode: bool, max_tokens: int, timeout: float) -> LLMResult:
        if not spec.base_url:
            raise LLMError("openai_compatible model has no base_url", retryable=False)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": spec.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
        }
        try:
            resp = httpx.post(f"{spec.base_url.rstrip('/')}/chat/completions", json=body, headers=headers,
                              timeout=timeout, follow_redirects=False)
        except httpx.HTTPError as e:
            raise LLMError(short_error(e)) from e
        if resp.status_code >= 400:
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise LLMError(f"HTTP {resp.status_code} from model endpoint", retryable=retryable)
        try:
            data = resp.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError("unexpected response shape from model endpoint") from e
        usage = data.get("usage") or {}
        return LLMResult(text=text, tokens_in=int(usage.get("prompt_tokens") or 0),
                         tokens_out=int(usage.get("completion_tokens") or 0))

    def stream(self, spec: ModelSpec, *, api_key: str, system: str, user: str, max_tokens: int,
               timeout: float, usage: dict, images: list[tuple[str, bytes]] | None = None) -> Iterator[str]:
        """Server-sent events from /chat/completions (OpenAI, OpenRouter, ...)."""
        if not spec.base_url:
            raise LLMError("openai_compatible model has no base_url", retryable=False)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        content: object = user
        if images:
            content = [{"type": "text", "text": user},
                       *({"type": "image_url", "image_url": {"url": f"data:{mime};base64,"
                                                                    f"{base64.b64encode(data).decode()}"}}
                         for mime, data in images)]
        body = {
            "model": spec.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            with httpx.stream("POST", f"{spec.base_url.rstrip('/')}/chat/completions", json=body, headers=headers,
                              timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
                              follow_redirects=False) as resp:
                if resp.status_code >= 400:
                    retryable = resp.status_code == 429 or resp.status_code >= 500
                    raise LLMError(f"HTTP {resp.status_code} from model endpoint", retryable=retryable)
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    u = data.get("usage") or {}
                    if u:
                        usage["tokens_in"] = int(u.get("prompt_tokens") or 0)
                        usage["tokens_out"] = int(u.get("completion_tokens") or 0)
                    choices = data.get("choices") or []
                    delta = (choices[0].get("delta") or {}).get("content") if choices else None
                    if delta:
                        yield delta
        except httpx.HTTPError as e:
            raise LLMError(short_error(e)) from e


ADAPTERS: dict[str, object] = {
    "gemini": GeminiAdapter(),
    "groq": GroqAdapter(),
    "huggingface": HuggingFaceAdapter(),
    "anthropic": AnthropicAdapter(),
    "openai_compatible": OpenAICompatibleAdapter(),
}

PROVIDERS = tuple(ADAPTERS)


def stream_text(adapter, spec: ModelSpec, *, api_key: str, system: str, user: str, max_tokens: int,
                timeout: float, usage: dict, images: list[tuple[str, bytes]] | None = None) -> Iterator[str]:
    """The adapter's stream(), or its complete() as a single chunk. Images
    (mime, bytes) go only to adapters that read them."""
    if hasattr(adapter, "stream"):
        kwargs = {"images": images} if images else {}
        yield from adapter.stream(spec, api_key=api_key, system=system, user=user, max_tokens=max_tokens,
                                  timeout=timeout, usage=usage, **kwargs)
        return
    if images:
        raise LLMError("this model does not read images", retryable=False)
    result = adapter.complete(spec, api_key=api_key, system=system, user=user, json_mode=False,
                              max_tokens=max_tokens, timeout=timeout)
    usage["tokens_in"], usage["tokens_out"] = result.tokens_in, result.tokens_out
    if result.text:
        yield result.text


def get_adapter(provider: str):
    try:
        return ADAPTERS[provider]
    except KeyError as e:
        raise LLMError(f"unknown provider {provider!r}", retryable=False) from e
