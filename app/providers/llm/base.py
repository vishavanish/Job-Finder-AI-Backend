"""
app/providers/llm/base.py
---------------------------
The one interface every LLM adapter implements (CLAUDE.md 7.9). Adapters
are synchronous because they run inside Celery tasks. They never pick a
model or a key themselves — app/services/ai_router.py hands them both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class LLMError(Exception):
    """A call failed. `retryable` = worth trying again later (rate limit,
    timeout, 5xx); the router moves to the next model either way."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model_id: str
    base_url: str | None = None
    max_output_tokens: int | None = None


@dataclass
class LLMResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    extra: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    def complete(self, spec: ModelSpec, *, api_key: str, system: str, user: str,
                 json_mode: bool, max_tokens: int, timeout: float) -> LLMResult: ...


def short_error(e: BaseException) -> str:
    """One line for logs/ledger. Never includes request bodies or keys."""
    text = f"{type(e).__name__}: {e}".replace("\n", " ")
    return text[:280]
