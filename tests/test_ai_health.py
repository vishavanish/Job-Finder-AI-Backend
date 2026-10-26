"""QA1 §3: how the AI router reacts to each kind of failure, the "never zero
models" probe and per-provider call slots. Error texts are real ones from the
production usage ledger (2026-10-21..24)."""
import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ai import AiModel
from app.services import ai_router

GROQ_429 = ("RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model "
            "`openai/gpt-oss-120b` in organization `org_x` service tier `on_demand`'}}")
GEMINI_429 = ("ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
              "current quota, please check your plan and billing details.'}}")
GEMINI_503 = ("ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently "
              "experiencing high demand.'}}")
GEMINI_504 = "ServerError: 504 DEADLINE_EXCEEDED. {'error': {'code': 504, 'message': 'Deadline expired'}}"
HF_402 = ("HfHubHTTPError: Client error '402 Payment Required' for url "
          "'https://router.huggingface.co/v1/chat/completions' (Request ID: Root=1-6adcff81-467009d7169ef81b06c)")
GEMINI_404 = ("ClientError: 404 NOT_FOUND. {'error': {'code': 404, 'message': 'This model models/gemini-2.5-flash "
              "is no longer available to new users.'}}")
# A 503 whose request id happens to contain "404" — must stay a passing error.
SERVER_503_WITH_ID = "ServerError: 503 UNAVAILABLE (Request ID: Root=1-6ad8f404-5b7d75cf7)"


@pytest.mark.parametrize("error, kind", [
    (GROQ_429, "rate_limit"),
    (GEMINI_429, "rate_limit"),
    (GEMINI_503, "transient"),
    (GEMINI_504, "transient"),
    (HF_402, "broken"),
    (GEMINI_404, "broken"),
    (SERVER_503_WITH_ID, "transient"),
    ("HTTP 401 from model endpoint", "broken"),
    ("HTTP 429 from model endpoint", "rate_limit"),
    ("empty response", "bad_reply"),
    ("response could not be parsed", "bad_reply"),
    ("ReadTimeout: timed out", "transient"),
])
def test_failure_kind(error, kind):
    assert ai_router.failure_kind(error) == kind


def test_rate_limit_pauses_briefly_and_doubles_while_it_repeats():
    s = get_settings()
    r = ai_router.get_redis()
    ai_router._record_failure("m-rate", GROQ_429)
    assert ai_router.pause_reason("m-rate") == "rate_limit"
    first = r.ttl(ai_router._open_key("m-rate"))
    assert 0 < first <= s.AI_RATE_LIMIT_PAUSE_SECONDS
    ai_router._record_failure("m-rate", GROQ_429)
    assert s.AI_RATE_LIMIT_PAUSE_SECONDS < r.ttl(ai_router._open_key("m-rate")) <= 2 * s.AI_RATE_LIMIT_PAUSE_SECONDS
    ai_router._record_success("m-rate")  # a good call resets the doubling
    ai_router.reset_breaker("m-rate")
    ai_router._record_failure("m-rate", GROQ_429)
    assert r.ttl(ai_router._open_key("m-rate")) <= s.AI_RATE_LIMIT_PAUSE_SECONDS


def test_rate_limit_pause_is_capped():
    s = get_settings()
    for _ in range(20):
        ai_router._record_failure("m-capped", GEMINI_429)
    assert ai_router.get_redis().ttl(ai_router._open_key("m-capped")) <= s.AI_RATE_LIMIT_MAX_PAUSE_SECONDS


def test_bad_reply_never_pauses_a_model():
    for _ in range(10):
        ai_router._record_failure("m-chatty", "response could not be parsed")
    assert not ai_router.breaker_open("m-chatty")


def test_broken_setup_pauses_for_an_hour():
    ai_router._record_failure("m-broke", HF_402)
    assert ai_router.pause_reason("m-broke") == "broken"
    assert ai_router.get_redis().ttl(ai_router._open_key("m-broke")) > get_settings().AI_BREAKER_COOLDOWN_SECONDS


def test_transient_errors_still_use_the_breaker():
    for _ in range(get_settings().AI_BREAKER_FAILURES - 1):
        ai_router._record_failure("m-flaky", GEMINI_503)
    assert not ai_router.breaker_open("m-flaky")
    ai_router._record_failure("m-flaky", GEMINI_504)
    assert ai_router.pause_reason("m-flaky") == "breaker"


def test_old_style_pause_value_reads_as_breaker():
    ai_router.get_redis().set(ai_router._open_key("m-old"), 1, ex=60)
    assert ai_router.pause_reason("m-old") == "breaker"


@pytest.fixture
def two_free_models():
    db = SessionLocal()
    try:
        made = []
        for model_id, priority in (("acme/first", 1), ("acme/second", 2)):
            m = AiModel(provider="openai_compatible", model_id=model_id, display_name=model_id,
                        base_url="https://api.acme.example/v1", tier="free", priority=priority,
                        enabled_global=True, key_scope="acme-test", tasks=[])  # keyless provider
            db.add(m)
            made.append(m)
        db.commit()
        ids = [m.id for m in made]
        # Only these two serve tasks here: switch the seeded models off for the test.
        others = db.query(AiModel).filter(AiModel.id.notin_(ids), AiModel.enabled_global.is_(True)).all()
        for m in others:
            m.enabled_global = False
        db.commit()
        yield ids
        for m in others:
            m.enabled_global = True
        db.query(AiModel).filter(AiModel.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _chain_ids(task="score"):
    db = SessionLocal()
    try:
        chain, skipped = ai_router.resolve_chain(db, user=None, task=task)
        return [m.model_id for m in chain], skipped
    finally:
        db.close()


def test_when_every_model_rests_the_one_back_soonest_is_tried(two_free_models):
    first, second = two_free_models
    ai_router._pause(first, "breaker", 300)
    ai_router._pause(second, "rate_limit", 30)
    chain, skipped = _chain_ids()
    assert chain == ["acme/second"]
    assert any("busy (rate limit)" in s for s in skipped)


def test_broken_models_are_never_probed(two_free_models):
    first, second = two_free_models
    ai_router._pause(first, "broken", 3600)
    ai_router._pause(second, "broken", 3600)
    chain, skipped = _chain_ids()
    assert chain == [] and all("set up wrong" in s for s in skipped)


def test_working_model_is_preferred_over_a_probe(two_free_models):
    first, _ = two_free_models
    ai_router._pause(first, "rate_limit", 5)
    assert _chain_ids()[0] == ["acme/second"]


def test_provider_slots_limit_parallel_calls(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AI_PROVIDER_CONCURRENCY", 2)
    monkeypatch.setattr(s, "AI_PROVIDER_SLOT_WAIT_SECONDS", 0)
    a = ai_router._take_slot("groq")
    b = ai_router._take_slot("groq")
    assert a and b and a != b
    assert ai_router._take_slot("groq") is None  # full: no wait left, the call goes ahead untracked
    assert ai_router._take_slot("gemini")  # other providers have their own slots
    ai_router._give_slot("groq", a)
    assert ai_router._take_slot("groq")


def test_provider_slots_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "AI_PROVIDER_CONCURRENCY", 0)
    assert ai_router._take_slot("groq") is None
