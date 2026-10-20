"""
app/services/ai_router.py
---------------------------
Picks which AI model runs a task and calls it (CLAUDE.md 7.9).

Order for a request: the user's chosen model for the task -> the admin's
default for the task -> every other model that serves the task, by
`priority` (lower first). A model is skipped when it is:
  - turned off by an admin,
  - paid while the user may not use paid models (needs the premium_models
    entitlement or the user's own key for that model's key scope),
  - paid while the user has "Free tools only" on,
  - missing a key (nothing stored and nothing in .env),
  - tripped by the circuit breaker (AI_BREAKER_FAILURES failures in a row
    -> skipped for AI_BREAKER_COOLDOWN_SECONDS, then tried again).

Every attempt, successful or not, is written to usage_ledger with tokens,
latency and cost. Keys are looked up here at run time (key_store) and
never logged.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.ai import AiModel, TaskModelDefault, UsageLedger, UserModelPref
from app.models.user import User
from app.providers.llm import LLMError, ModelSpec, get_adapter, stream_text
from app.providers.llm.base import short_error
from app.services import entitlements, key_store

logger = logging.getLogger("job_finder_api.ai_router")

# Task types the router serves (CLAUDE.md 7.9). stt/tts are speech tasks (P7): a model serves
# them only when its `tasks` list names them; `tasks == []` means "every text task".
TASKS = ("score", "resume_parse", "title_suggest", "resume_tailor", "cover_letter", "form_answer",
         "email_classify", "interview_chat", "interview_eval", "copilot_answer", "copilot_expand",
         "question_gate", "stt", "tts")
SPEECH_TASKS = frozenset({"stt", "tts"})
# Providers that only do speech (no text adapter in app/providers/llm).
SPEECH_ONLY_PROVIDERS = frozenset({"browser", "deepgram"})

# Providers that cannot run without a key (openai_compatible may be a keyless local server).
KEY_REQUIRED = {"gemini", "groq", "huggingface", "anthropic", "deepgram"}


class NoModelAvailable(Exception):
    """Every candidate was skipped or failed. `attempts` lists why, per model."""

    def __init__(self, task: str, attempts: list[str]):
        detail = "; ".join(attempts) if attempts else "no enabled model serves this task"
        super().__init__(f"No AI model could run '{task}': {detail}")
        self.attempts = attempts


@dataclass
class RouterResult:
    text: str
    model: AiModel
    key_source: str
    tokens_in: int
    tokens_out: int
    cost_usd: float

    @property
    def engine(self) -> str:
        return f"{self.model.provider}:{self.model.model_id}"


@dataclass(frozen=True)
class Access:
    allowed: bool
    reason: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def serves(model: AiModel, task: str) -> bool:
    if task in SPEECH_TASKS:
        return task in (model.tasks or [])
    if model.provider in SPEECH_ONLY_PROVIDERS:
        return False
    tasks = [t for t in (model.tasks or []) if t not in SPEECH_TASKS]
    if model.tasks and not tasks:
        return False  # a speech-only row (e.g. Whisper) never serves text
    return not tasks or task in tasks


# ---------------------------------------------------------------- breaker

def _fail_key(model_id: str) -> str:
    return f"ai_fail:{model_id}"


def _open_key(model_id: str) -> str:
    return f"ai_open:{model_id}"


def breaker_open(model_id: str) -> bool:
    return bool(get_redis().exists(_open_key(model_id)))


def _record_failure(model_id: str) -> None:
    s = get_settings()
    r = get_redis()
    fails = r.incr(_fail_key(model_id))
    r.expire(_fail_key(model_id), s.AI_BREAKER_COOLDOWN_SECONDS * 2)
    if fails >= s.AI_BREAKER_FAILURES:
        r.set(_open_key(model_id), 1, ex=s.AI_BREAKER_COOLDOWN_SECONDS)
        r.delete(_fail_key(model_id))
        logger.warning("ai_router: breaker opened for model=%s", model_id)


def _record_success(model_id: str) -> None:
    get_redis().delete(_fail_key(model_id))


def reset_breaker(model_id: str) -> None:
    get_redis().delete(_fail_key(model_id), _open_key(model_id))


# ---------------------------------------------------------------- access

def may_use_paid(db: Session, user: User | None) -> bool:
    return bool(user) and entitlements.effective(db, user.id, "premium_models").enabled


def access(db: Session, user: User | None, model: AiModel, *, user_scopes: set[str] | None = None,
           extra_scopes: set[str] | None = None, free_only: bool = False) -> Access:
    """Whether `user` may use `model` now, with the reason when not. Shown
    to users (GET /models) and applied by the router. free_only forces the
    "Free tools only" rule for one call (a voice session past its cost cap)."""
    if not model.enabled_global:
        return Access(False, "Turned off by the admin.")
    own_key = model.key_scope in (user_scopes or set()) or model.key_scope in (extra_scopes or set())
    if model.tier == "paid":
        if free_only:
            return Access(False, "Paid model — this session reached its cost cap.")
        if user is not None and user.free_only_mode:
            return Access(False, "Paid model — \"Free tools only\" is on.")
        if not (own_key or may_use_paid(db, user)):
            return Access(False, f"Paid model — needs the premium AI models feature or your own "
                                 f"{model.key_scope} key.")
    if model.provider in KEY_REQUIRED and not own_key and not key_store.platform_key_available(db, model.key_scope):
        return Access(False, f"No {model.key_scope} API key is set up yet.")
    return Access(True)


# ---------------------------------------------------------------- chain

def resolve_chain(db: Session, *, user: User | None, task: str, extra_scopes: set[str] | None = None,
                  free_only: bool = False) -> tuple[list[AiModel], list[str]]:
    """(models to try in order, reasons for the ones skipped)."""
    models = db.query(AiModel).order_by(AiModel.priority, AiModel.display_name).all()
    by_id = {m.id: m for m in models}
    ordered: list[AiModel] = []

    if user is not None:
        pref = db.query(UserModelPref).filter(UserModelPref.user_id == user.id,
                                              UserModelPref.task == task).first()
        if pref and pref.model_ref in by_id:
            ordered.append(by_id[pref.model_ref])
    default = db.get(TaskModelDefault, task)
    if default and default.model_ref in by_id:
        ordered.append(by_id[default.model_ref])
    ordered.extend(models)

    user_scopes = key_store.user_key_scopes(db, user.id if user else None)
    chain: list[AiModel] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for m in ordered:
        if m.id in seen or not serves(m, task):
            continue
        seen.add(m.id)
        verdict = access(db, user, m, user_scopes=user_scopes, extra_scopes=extra_scopes, free_only=free_only)
        if not verdict.allowed:
            if m.enabled_global:
                skipped.append(f"{m.display_name}: {verdict.reason}")
            continue
        if breaker_open(m.id):
            skipped.append(f"{m.display_name}: paused after repeated failures")
            continue
        chain.append(m)
    return chain, skipped


# ---------------------------------------------------------------- calls

def _cost(model: AiModel, tokens_in: int, tokens_out: int) -> float:
    return round(tokens_in / 1000 * (model.cost_per_1k_in or 0) + tokens_out / 1000 * (model.cost_per_1k_out or 0), 6)


def record_usage(db: Session, *, user_id: str | None, task: str, model: AiModel, key_source: str,
                 tokens_in: int, tokens_out: int, latency_ms: int, success: bool, error: str | None = None,
                 ref: str | None = None) -> float:
    """Ledger one call made outside call_model (speech, streams). Returns its cost."""
    cost = _ledger(db, user_id=user_id, task=task, model=model, key_source=key_source, tokens_in=tokens_in,
                   tokens_out=tokens_out, latency_ms=latency_ms, success=success, error=error, ref=ref)
    if success:
        _record_success(model.id)
    else:
        _record_failure(model.id)
    return cost


def resolve_key(db: Session, *, user_id: str | None, model: AiModel) -> tuple[str, str]:
    """(api key, source) for `model`; ("", "none") when nothing is stored."""
    resolved = key_store.resolve(db, user_id=user_id, scope=model.key_scope)
    return (resolved.value, resolved.source) if resolved else ("", "none")


def _ledger(db: Session, *, user_id: str | None, task: str, model: AiModel, key_source: str,
            tokens_in: int, tokens_out: int, latency_ms: int, success: bool, error: str | None,
            ref: str | None) -> float:
    cost = _cost(model, tokens_in, tokens_out) if success or tokens_in or tokens_out else 0.0
    db.add(UsageLedger(
        user_id=user_id, task=task, model_ref=model.id, provider=model.provider, model_id=model.model_id,
        key_source=key_source, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms,
        cost_usd=cost, success=success, error=error, ref=ref,
    ))
    if success:
        model.consecutive_failures = 0
        model.last_success_at = _now()
    else:
        model.consecutive_failures = (model.consecutive_failures or 0) + 1
        model.last_error_at = _now()
        model.last_error = (error or "")[:300]
    db.commit()
    return cost


def call_model(db: Session, model: AiModel, *, user_id: str | None, task: str, system: str, user_prompt: str,
               api_key: str, key_source: str, json_mode: bool = False, max_tokens: int = 2000,
               ref: str | None = None, validate: Callable[[str], bool] | None = None) -> RouterResult:
    """One attempt on one model, ledgered. Raises LLMError on failure."""
    adapter = get_adapter(model.provider)
    spec = ModelSpec(model.provider, model.model_id, model.base_url, model.max_output_tokens)
    limit = min(max_tokens, model.max_output_tokens) if model.max_output_tokens else max_tokens
    started = time.monotonic()
    try:
        result = adapter.complete(spec, api_key=api_key, system=system, user=user_prompt, json_mode=json_mode,
                                  max_tokens=limit, timeout=float(get_settings().AI_REQUEST_TIMEOUT_SECONDS))
        if not result.text.strip():
            raise LLMError("empty response")
        if validate is not None and not validate(result.text):
            raise LLMError("response could not be parsed")
    except LLMError as e:
        latency = int((time.monotonic() - started) * 1000)
        _ledger(db, user_id=user_id, task=task, model=model, key_source=key_source, tokens_in=0, tokens_out=0,
                latency_ms=latency, success=False, error=str(e), ref=ref)
        _record_failure(model.id)
        raise
    except Exception as e:  # noqa: BLE001 — an adapter bug must not stop the chain
        latency = int((time.monotonic() - started) * 1000)
        _ledger(db, user_id=user_id, task=task, model=model, key_source=key_source, tokens_in=0, tokens_out=0,
                latency_ms=latency, success=False, error=short_error(e), ref=ref)
        _record_failure(model.id)
        raise LLMError(short_error(e)) from e

    latency = int((time.monotonic() - started) * 1000)
    cost = _ledger(db, user_id=user_id, task=task, model=model, key_source=key_source,
                   tokens_in=result.tokens_in, tokens_out=result.tokens_out, latency_ms=latency,
                   success=True, error=None, ref=ref)
    _record_success(model.id)
    return RouterResult(result.text, model, key_source, result.tokens_in, result.tokens_out, cost)


def complete(db: Session, *, user_id: str | None, task: str, system: str, user_prompt: str,
             json_mode: bool = False, max_tokens: int = 2000, ref: str | None = None,
             validate: Callable[[str], bool] | None = None,
             request_keys: dict[str, str] | None = None,
             progress: Callable[[str], None] | None = None) -> RouterResult:
    """Runs `task` on the first model in the chain that succeeds.

    request_keys: one-run keys sent in a request body (legacy /score fields),
    by key scope. They act like the user's own key for this call only."""
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    request_keys = {k: v for k, v in (request_keys or {}).items() if v}
    user = db.get(User, user_id) if user_id else None
    chain, attempts = resolve_chain(db, user=user, task=task, extra_scopes=set(request_keys))

    for model in chain:
        if model.key_scope in request_keys:
            api_key, source = request_keys[model.key_scope], "request"
        else:
            resolved = key_store.resolve(db, user_id=user_id, scope=model.key_scope)
            api_key, source = (resolved.value, resolved.source) if resolved else ("", "none")
        if progress:
            progress(f"trying {model.display_name} for {task}")
        try:
            return call_model(db, model, user_id=user_id, task=task, system=system, user_prompt=user_prompt,
                              api_key=api_key, key_source=source, json_mode=json_mode, max_tokens=max_tokens,
                              ref=ref, validate=validate)
        except LLMError as e:
            logger.warning("ai_router: %s failed for task=%s user=%s: %s", model.model_id, task, user_id, e)
            attempts.append(f"{model.display_name}: {e}")
            if progress:
                progress(f"{model.display_name} failed ({e}) — trying the next model")
    raise NoModelAvailable(task, attempts)


# ---------------------------------------------------------------- streams (P7, voice)

@dataclass
class StreamOutcome:
    """Filled by stream(): which model answered, the full text and its cost."""
    model: AiModel | None = None
    key_source: str = ""
    text: str = ""
    cost_usd: float = 0.0
    first_token_ms: int | None = None
    cancelled: bool = False
    attempts: list[str] | None = None


def stream(db: Session, *, user_id: str | None, task: str, system: str, user_prompt: str,
           outcome: StreamOutcome, max_tokens: int = 800, ref: str | None = None, free_only: bool = False,
           timeout: float | None = None, cancel: threading.Event | None = None,
           images: list[tuple[str, bytes]] | None = None) -> Iterator[str]:
    """Streams `task` from the first model in the chain that produces a
    first chunk. A model that fails BEFORE its first chunk is skipped (the
    next one is tried); a failure after that raises LLMError — the caller
    has already shown part of the answer. `cancel` stops the stream between
    chunks (the HTTP stream is closed); a cancelled call is still ledgered.
    Raises NoModelAvailable when nothing produced a first chunk."""
    if task not in TASKS or task in SPEECH_TASKS:
        raise ValueError(f"unknown text task {task!r}")
    user = db.get(User, user_id) if user_id else None
    chain, attempts = resolve_chain(db, user=user, task=task, free_only=free_only)
    outcome.attempts = attempts
    limit_timeout = timeout or float(get_settings().AI_REQUEST_TIMEOUT_SECONDS)

    for model in chain:
        api_key, source = resolve_key(db, user_id=user_id, model=model)
        adapter = get_adapter(model.provider)
        spec = ModelSpec(model.provider, model.model_id, model.base_url, model.max_output_tokens)
        limit = min(max_tokens, model.max_output_tokens) if model.max_output_tokens else max_tokens
        usage: dict = {}
        started = time.monotonic()
        chunks: list[str] = []
        gen = stream_text(adapter, spec, api_key=api_key, system=system, user=user_prompt, max_tokens=limit,
                          timeout=limit_timeout, usage=usage, images=images)
        try:
            for chunk in gen:
                if not chunks:
                    outcome.first_token_ms = int((time.monotonic() - started) * 1000)
                    outcome.model, outcome.key_source = model, source
                chunks.append(chunk)
                yield chunk
                if cancel is not None and cancel.is_set():
                    outcome.cancelled = True
                    break
        except LLMError as e:
            gen.close()
            latency = int((time.monotonic() - started) * 1000)
            _ledger(db, user_id=user_id, task=task, model=model, key_source=source, tokens_in=0,
                    tokens_out=_estimate_tokens("".join(chunks)), latency_ms=latency, success=False, error=str(e),
                    ref=ref)
            _record_failure(model.id)
            if chunks:
                raise
            logger.warning("ai_router: stream %s failed for task=%s user=%s: %s", model.model_id, task, user_id, e)
            attempts.append(f"{model.display_name}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — an adapter bug must not stop the chain
            gen.close()
            _record_failure(model.id)
            if chunks:
                raise LLMError(short_error(e)) from e
            attempts.append(f"{model.display_name}: {short_error(e)}")
            continue
        finally:
            gen.close()

        if not chunks:
            attempts.append(f"{model.display_name}: empty response")
            _record_failure(model.id)
            continue
        text = "".join(chunks)
        latency = int((time.monotonic() - started) * 1000)
        tokens_in = int(usage.get("tokens_in") or _estimate_tokens(system + user_prompt))
        tokens_out = int(usage.get("tokens_out") or _estimate_tokens(text))
        outcome.text = text
        outcome.cost_usd = _ledger(db, user_id=user_id, task=task, model=model, key_source=source,
                                   tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency, success=True,
                                   error=None, ref=ref)
        _record_success(model.id)
        return
    raise NoModelAvailable(task, attempts)


def _estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)
