"""Live Copilot speed (2026-10-31): no optional thinking on live voice tasks, a first-token deadline per
model, the fast model first, and the timing line on each card."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.db import SessionLocal, engine
from app.models.ai import AiModel, TaskModelDefault
from app.providers.llm import ModelSpec, adapters
from app.services import ai_router

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0020_copilot_fast_default.py"


def spec(model_id: str, provider: str = "gemini", fast: bool = True) -> ModelSpec:
    return ModelSpec(provider, model_id, fast=fast)


def test_fast_tasks_ask_each_model_for_the_least_thinking():
    assert adapters.gemini_thinking(spec("gemini-3.8-flash")) == {"thinking_level": "minimal"}
    assert adapters.gemini_thinking(spec("gemini-3.1-pro")) == {"thinking_level": "low"}
    assert adapters.gemini_thinking(spec("gemini-2.5-flash")) == {"thinking_budget": 0}
    assert adapters.gemini_thinking(spec("gemini-2.5-pro")) is None  # Pro can't switch it off
    assert adapters.gemini_thinking(spec("gemini-2.0-flash")) is None
    assert adapters.gemini_thinking(spec("gemini-3.8-flash", fast=False)) is None
    assert adapters.groq_reasoning(spec("openai/gpt-oss-120b", "groq")) == {"reasoning_effort": "low",
                                                                           "include_reasoning": False}
    assert adapters.groq_reasoning(spec("llama-3.3-70b-versatile", "groq")) == {}
    assert adapters.groq_reasoning(spec("openai/gpt-oss-120b", "groq", fast=False)) == {}


def test_gemini_stream_retries_without_thinking_when_the_model_refuses_it(monkeypatch):
    from google import genai

    configs: list[dict] = []

    class Models:
        def generate_content_stream(self, *, model, contents, config):
            configs.append(dict(config))
            if "thinking_config" in config:
                raise ValueError("400 INVALID_ARGUMENT: thinking_level is not supported for this model")
            yield SimpleNamespace(text="Hi", usage_metadata=None)

    monkeypatch.setattr(genai, "Client", lambda **kw: SimpleNamespace(models=Models()))
    out = list(adapters.GeminiAdapter().stream(spec("gemini-3.8-flash"), api_key="k", system="s", user="u",
                                               max_tokens=50, timeout=5, usage={}))
    assert out == ["Hi"]
    assert configs[0]["thinking_config"] == {"thinking_level": "minimal"} and "thinking_config" not in configs[1]


def _two_models(monkeypatch, stream_text):
    a = AiModel(id="m-a", provider="groq", model_id="a", display_name="A", tier="free", tasks=[], key_scope="groq",
                cost_per_1k_in=0, cost_per_1k_out=0, priority=1, enabled_global=True, consecutive_failures=0)
    b = AiModel(id="m-b", provider="groq", model_id="b", display_name="B", tier="free", tasks=[], key_scope="groq",
                cost_per_1k_in=0, cost_per_1k_out=0, priority=2, enabled_global=True, consecutive_failures=0)
    monkeypatch.setattr(ai_router, "resolve_chain", lambda db, **kw: ([a, b], []))
    monkeypatch.setattr(ai_router, "resolve_key", lambda db, **kw: ("k", "platform"))
    monkeypatch.setattr(ai_router, "get_adapter", lambda provider: object())
    monkeypatch.setattr(ai_router, "stream_text", stream_text)
    monkeypatch.setattr(ai_router, "_ledger", lambda db, **kw: 0.0)
    return a, b


def test_router_marks_live_voice_tasks_fast(monkeypatch, make_user):
    user_id, _ = make_user()
    seen: dict[str, bool] = {}

    def stream_text(adapter, spec, **kw):
        seen[kw["system"]] = spec.fast
        yield "ok"

    _two_models(monkeypatch, stream_text)
    db = SessionLocal()
    try:
        for task in ("copilot_answer", "copilot_expand", "interview_chat", "interview_eval"):
            list(ai_router.stream(db, user_id=user_id, task=task, system=task, user_prompt="u",
                                  outcome=ai_router.StreamOutcome()))
    finally:
        db.close()
    assert seen == {"copilot_answer": True, "copilot_expand": False, "interview_chat": True,
                    "interview_eval": False}


def test_a_model_without_a_first_token_in_time_hands_over_to_the_next(monkeypatch, make_user):
    user_id, _ = make_user()

    def stream_text(adapter, spec, **kw):
        if spec.model_id == "a":
            time.sleep(1.0)  # stuck model
        yield f"from {spec.model_id}"

    _, b = _two_models(monkeypatch, stream_text)
    outcome = ai_router.StreamOutcome()
    db = SessionLocal()
    started = time.monotonic()
    try:
        chunks = list(ai_router.stream(db, user_id=user_id, task="copilot_answer", system="s", user_prompt="u",
                                       outcome=outcome, first_token_timeout=0.2))
    finally:
        db.close()
    assert chunks == ["from b"] and outcome.model is b
    assert time.monotonic() - started < 0.8
    assert any("no first token" in x for x in outcome.attempts)


def test_a_slow_stream_after_its_first_token_is_not_cut(monkeypatch, make_user):
    user_id, _ = make_user()

    def stream_text(adapter, spec, **kw):
        yield "one "
        time.sleep(0.4)
        yield "two"

    _two_models(monkeypatch, stream_text)
    db = SessionLocal()
    try:
        chunks = list(ai_router.stream(db, user_id=user_id, task="copilot_answer", system="s", user_prompt="u",
                                       outcome=ai_router.StreamOutcome(), first_token_timeout=0.2))
    finally:
        db.close()
    assert chunks == ["one ", "two"]


def _load_migration():
    mod_spec = importlib.util.spec_from_file_location("m0020", MIGRATION)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return mod


def test_migration_makes_groq_the_copilot_default_only_where_none_is_set(monkeypatch):
    m = _load_migration()
    db = SessionLocal()
    try:
        groq = db.query(AiModel).filter(AiModel.provider == "groq", AiModel.model_id == "openai/gpt-oss-120b").first()
        if groq is None:
            pytest.skip("registry seed changed")
        other = db.query(AiModel).filter(AiModel.id != groq.id, AiModel.provider != "groq").first()
        db.query(TaskModelDefault).filter(TaskModelDefault.task.in_(m.TASKS)).delete(synchronize_session=False)
        db.add(TaskModelDefault(task="question_gate", model_ref=other.id, updated_by="admin"))
        db.commit()
        with engine.begin() as conn:
            monkeypatch.setattr(m.op, "get_bind", lambda: conn, raising=False)
            m.upgrade()
        db.expire_all()
        assert db.get(TaskModelDefault, "copilot_answer").model_ref == groq.id
        assert db.get(TaskModelDefault, "question_gate").model_ref == other.id  # the admin's choice stays
        with engine.begin() as conn:
            monkeypatch.setattr(m.op, "get_bind", lambda: conn, raising=False)
            m.downgrade()
        db.expire_all()
        assert db.get(TaskModelDefault, "copilot_answer") is None
        assert db.get(TaskModelDefault, "question_gate") is not None
    finally:
        db.query(TaskModelDefault).filter(TaskModelDefault.task.in_(m.TASKS)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_qwen_on_groq_skips_thinking_on_live_tasks_and_hides_it_elsewhere():
    assert adapters.groq_reasoning(spec("qwen/qwen3.8-27b", "groq")) == {"reasoning_effort": "none"}
    assert adapters.groq_reasoning(spec("qwen/qwen3.8-27b", "groq", fast=False)) == {"reasoning_format": "hidden"}


def _load(name: str):
    path = MIGRATION.parent / name
    mod_spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return mod


def test_migration_0021_moves_the_copilot_default_to_qwen_but_keeps_an_admin_choice(monkeypatch):
    m = _load("0021_copilot_qwen.py")
    db = SessionLocal()
    try:
        qwen = db.query(AiModel).filter(AiModel.provider == "groq", AiModel.model_id == m.MODEL_ID).one()
        assert qwen.tasks == ["copilot_answer", "question_gate"] and qwen.tier == "free"
        gpt = db.query(AiModel).filter(AiModel.provider == "groq", AiModel.model_id == "openai/gpt-oss-120b").one()
        db.query(TaskModelDefault).filter(TaskModelDefault.task.in_(m.TASKS)).delete(synchronize_session=False)
        db.add(TaskModelDefault(task="copilot_answer", model_ref=gpt.id, updated_by="migration 0020"))
        db.add(TaskModelDefault(task="question_gate", model_ref=gpt.id, updated_by="admin"))
        db.commit()
        with engine.begin() as conn:
            monkeypatch.setattr(m.op, "get_bind", lambda: conn, raising=False)
            m.upgrade()
        db.expire_all()
        assert db.get(TaskModelDefault, "copilot_answer").model_ref == qwen.id
        assert db.get(TaskModelDefault, "question_gate").model_ref == gpt.id  # the admin's choice stays
    finally:
        db.query(TaskModelDefault).filter(TaskModelDefault.task.in_(m.TASKS)).delete(synchronize_session=False)
        db.commit()
        db.close()


@pytest.mark.parametrize("question, qtype, expect", [
    ("Write a program to find duplicate elements in a list.", "coding", "then a line starting 'Approach:'"),
    ("How does a Kubernetes rolling update work?", "devops", "kubectl"),
    ("What is the difference between useEffect and useLayoutEffect in React?", "frontend", "concept"),
    ("How would you make a slow REST API endpoint faster?", "backend", "API, database, cache, queue"),
])
def test_each_technical_type_gets_its_own_answer_format(question, qtype, expect):
    from app.services import copilot_session as cs, question_gate, voice_context
    r = question_gate.classify(question)
    assert r.is_question and r.qtype == qtype
    sess = cs.CopilotSession.__new__(cs.CopilotSession)
    sess.ctx = voice_context.SessionContext(role="Software engineer")
    sess.cards, sess.length, sess.style, sess.language, sess.context = {}, "short", "bullets", "en", []
    system, _, _ = sess._prompts(cs.Card("x", question, r.qtype), "normal")
    assert expect in system and "Not in your profile:" not in system
    if qtype == "coding":
        assert system.index("code block") < system.index("Approach:") < system.index("Complexity:")
