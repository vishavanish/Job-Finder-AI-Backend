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
"""
from __future__ import annotations

import httpx

from app.providers.llm.base import LLMError, LLMResult, ModelSpec, short_error

TEMPERATURE = 0.1


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
        try:
            client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout * 1000)})
            resp = client.models.generate_content(model=spec.model_id, contents=user, config=config)
        except Exception as e:  # noqa: BLE001 — SDK raises many types; the router only needs "failed"
            raise LLMError(short_error(e)) from e
        usage = getattr(resp, "usage_metadata", None)
        return LLMResult(
            text=resp.text or "",
            tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
            tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
        )


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
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(short_error(e)) from e
        return _openai_style_result(resp)


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


ADAPTERS: dict[str, object] = {
    "gemini": GeminiAdapter(),
    "groq": GroqAdapter(),
    "huggingface": HuggingFaceAdapter(),
    "anthropic": AnthropicAdapter(),
    "openai_compatible": OpenAICompatibleAdapter(),
}

PROVIDERS = tuple(ADAPTERS)


def get_adapter(provider: str):
    try:
        return ADAPTERS[provider]
    except KeyError as e:
        raise LLMError(f"unknown provider {provider!r}", retryable=False) from e
