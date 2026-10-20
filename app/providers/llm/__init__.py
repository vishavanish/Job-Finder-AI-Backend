"""LLM adapters behind one interface (CLAUDE.md 7.9). Chosen by app/services/ai_router.py."""
from app.providers.llm.adapters import ADAPTERS, PROVIDERS, get_adapter, stream_text  # noqa: F401
from app.providers.llm.base import LLMError, LLMResult, ModelSpec  # noqa: F401
