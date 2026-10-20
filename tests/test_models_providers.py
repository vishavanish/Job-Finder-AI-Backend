"""Phase 2 (CLAUDE.md 7.4, 7.9): model registry, router, key store, usage
ledger, job-source catalog and Free tools only. No network: adapters are
replaced by recorders."""
import json

import pytest

from app.core import celery_tasks
from app.core.db import SessionLocal
from app.models.admin import AuditLog
from app.models.ai import (
    AiModel, ProviderCatalog, Secret, TaskModelDefault, UsageLedger, UserModelPref, UserProviderSetting,
)
from app.providers.llm import LLMError, LLMResult, adapters
from app.services import ai_router, key_store

SEED_MODEL_IDS = {"gemini-2.5-flash", "openai/gpt-oss-120b", "Qwen/Qwen3-8B",
                  # P7 speech rows (on) and the OpenRouter / OpenAI rows (seeded switched off)
                  "whisper-large-v3-turbo", "speech-synthesis", "openai/gpt-oss-120b:free", "gpt-5-mini",
                  "gpt-4o-mini-transcribe", "gpt-4o-mini-tts"}
SEEDED_OFF = {"openai/gpt-oss-120b:free", "gpt-5-mini", "gpt-4o-mini-transcribe", "gpt-4o-mini-tts"}
USER_KEY = "sk-user-own-key-1234567890abcd"
PLATFORM_KEY = "sk-platform-key-0987654321wxyz"
SCORE_REPLY = json.dumps([{"index": 0, "score": 88, "reason": "Strong Python overlap."}])


class FakeAdapter:
    """Stands in for every provider: records calls, replies or fails on demand."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_models: set[str] = set()
        self.reply = SCORE_REPLY

    def complete(self, spec, *, api_key, system, user, json_mode, max_tokens, timeout):
        self.calls.append({"model_id": spec.model_id, "api_key": api_key, "base_url": spec.base_url})
        if spec.model_id in self.fail_models:
            raise LLMError("HTTP 503 from model endpoint")
        return LLMResult(text=self.reply, tokens_in=1000, tokens_out=200)


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeAdapter()
    for name in list(adapters.ADAPTERS):
        monkeypatch.setitem(adapters.ADAPTERS, name, fake)
    return fake


@pytest.fixture(autouse=True)
def _quiet_progress(monkeypatch):
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    db = SessionLocal()
    try:
        for table in (UserModelPref, TaskModelDefault, UsageLedger, Secret, UserProviderSetting):
            db.query(table).delete()
        db.query(AiModel).filter(AiModel.model_id.notin_(SEED_MODEL_IDS)).delete(synchronize_session=False)
        for m in db.query(AiModel).all():
            m.enabled_global, m.consecutive_failures = m.model_id not in SEEDED_OFF, 0
        for r in db.query(ProviderCatalog).all():
            r.enabled_global = True
        db.commit()
    finally:
        db.close()


def _add_model(client, headers, **overrides) -> dict:
    body = {"provider": "openai_compatible", "model_id": "acme/fast-1", "display_name": "Acme Fast",
            "base_url": "https://api.acme-llm.example/v1", "tier": "free", "priority": 5, **overrides}
    resp = client.post("/api/v1/admin/models", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set_platform_key(client, headers, scope, value=PLATFORM_KEY):
    resp = client.post("/api/v1/admin/keys", json={"scope": scope, "key": value}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _score(user_id: str, **extra) -> dict:
    jobs = [{"title": "Backend Engineer", "company": "Acme", "description": "python fastapi", "source": "LinkedIn"}]
    params = {"user_id": user_id, "resume_summary": "Python dev", "skills": ["python"],
              "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 0, **extra}
    return celery_tasks.score_task.run(jobs, params)


# ---------------------------------------------------------------- acceptance

def test_admin_adds_model_user_selects_it_and_scorer_uses_it(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    model = _add_model(client, admin, priority=500)  # last in priority: only used because the user picks it
    _set_platform_key(client, admin, model["key_scope"])
    assert model["key_scope"] == "acme-llm"

    user_id, headers = make_user()
    catalog = client.get("/api/v1/models", headers=headers).json()
    listed = {m["id"]: m for m in catalog["models"]}
    assert listed[model["id"]]["available"] is True

    resp = client.put("/api/v1/models/preferences", json={"preferences": {"score": model["id"]}}, headers=headers)
    assert resp.status_code == 200 and resp.json()["preferences"] == {"score": model["id"]}

    result = _score(user_id)
    assert result["ranking_engine_used"] == "openai_compatible:acme/fast-1"
    assert result["ranked_jobs"][0]["llm_score"] == 88
    assert fake_llm.calls[0] == {"model_id": "acme/fast-1", "api_key": PLATFORM_KEY,
                                 "base_url": "https://api.acme-llm.example/v1"}

    usage = client.get("/api/v1/usage", headers=headers).json()
    assert usage["totals"]["calls"] == 1 and usage["totals"]["tokens_in"] == 1000
    assert usage["by_model"][0]["model_id"] == "acme/fast-1"


def test_disabling_a_model_hides_it_and_users_fall_back(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    picked = _add_model(client, admin, model_id="acme/picked", priority=50)
    fallback = _add_model(client, admin, model_id="acme/fallback", priority=60)
    _set_platform_key(client, admin, "acme-llm")
    user_id, headers = make_user()
    client.put("/api/v1/models/preferences", json={"preferences": {"score": picked["id"]}}, headers=headers)

    assert client.patch(f"/api/v1/admin/models/{picked['id']}", json={"enabled_global": False},
                        headers=admin).status_code == 200
    ids = {m["id"] for m in client.get("/api/v1/models", headers=headers).json()["models"]}
    assert picked["id"] not in ids and fallback["id"] in ids
    assert _score(user_id)["ranking_engine_used"] == "openai_compatible:acme/fallback"


# ---------------------------------------------------------------- router

def test_chain_order_is_user_choice_then_admin_default_then_priority(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    low = _add_model(client, admin, model_id="acme/low", priority=1)
    default = _add_model(client, admin, model_id="acme/default", priority=90)
    chosen = _add_model(client, admin, model_id="acme/chosen", priority=99)
    _set_platform_key(client, admin, "acme-llm")
    assert client.put("/api/v1/admin/models/defaults", json={"defaults": {"score": default["id"]}},
                      headers=admin).status_code == 200
    user_id, headers = make_user()

    db = SessionLocal()
    try:
        from app.models.user import User
        chain, _ = ai_router.resolve_chain(db, user=db.get(User, user_id), task="score")
        assert [m.model_id for m in chain][:2] == ["acme/default", "acme/low"]
        client.put("/api/v1/models/preferences", json={"preferences": {"score": chosen["id"]}}, headers=headers)
        db.expire_all()
        chain, _ = ai_router.resolve_chain(db, user=db.get(User, user_id), task="score")
        assert [m.model_id for m in chain][:3] == ["acme/chosen", "acme/default", "acme/low"]
    finally:
        db.close()
    assert low["id"]


def test_failed_model_falls_through_and_breaker_pauses_it(client, make_admin, make_user, fake_llm, fake_redis):
    _, admin = make_admin()
    bad = _add_model(client, admin, model_id="acme/flaky", priority=1)
    _add_model(client, admin, model_id="acme/steady", priority=2)
    _set_platform_key(client, admin, "acme-llm")
    user_id, _ = make_user()
    fake_llm.fail_models.add("acme/flaky")

    for _ in range(3):
        assert _score(user_id)["ranking_engine_used"] == "openai_compatible:acme/steady"
    assert ai_router.breaker_open(bad["id"])
    fake_llm.calls.clear()
    _score(user_id)
    assert [c["model_id"] for c in fake_llm.calls] == ["acme/steady"]  # paused model not even tried

    db = SessionLocal()
    try:
        failed = db.query(UsageLedger).filter(UsageLedger.model_id == "acme/flaky").all()
        assert len(failed) == 3 and not any(r.success for r in failed)
    finally:
        db.close()


def test_unparseable_reply_counts_as_failure(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    _add_model(client, admin)
    _set_platform_key(client, admin, "acme-llm")
    user_id, _ = make_user()
    fake_llm.reply = "Sure! Here are the scores: great fit."
    result = _score(user_id)
    assert result["ranking_engine_used"] is None and result["ranked_jobs"] == []


# ---------------------------------------------------------------- paid models

def test_paid_model_needs_premium_or_own_key(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    paid = _add_model(client, admin, provider="anthropic", model_id="claude-opus-5-5", display_name="Claude Opus 5.5",
                      base_url=None, tier="paid", cost_per_1k_in=0.004, cost_per_1k_out=0.02)
    assert paid["key_scope"] == "anthropic"
    _set_platform_key(client, admin, "anthropic")
    user_id, headers = make_user()

    def entry():
        return next(m for m in client.get("/api/v1/models", headers=headers).json()["models"] if m["id"] == paid["id"])

    assert entry()["available"] is False and "premium" in entry()["reason"]
    assert client.put("/api/v1/models/preferences", json={"preferences": {"score": paid["id"]}},
                      headers=headers).status_code == 422

    # Own key unlocks it, and the user's key (not the platform key) is used.
    assert client.post("/api/v1/keys", json={"scope": "anthropic", "key": USER_KEY}, headers=headers).status_code == 201
    assert entry()["available"] is True and entry()["uses_own_key"] is True
    client.put("/api/v1/models/preferences", json={"preferences": {"score": paid["id"]}}, headers=headers)
    result = _score(user_id)
    assert result["ranking_engine_used"] == "anthropic:claude-opus-5-5"
    assert fake_llm.calls[-1]["api_key"] == USER_KEY

    db = SessionLocal()
    try:
        row = db.query(UsageLedger).filter(UsageLedger.user_id == user_id).one()
        assert row.key_source == "byok" and row.cost_usd == pytest.approx(0.004 + 0.004)
    finally:
        db.close()


def test_premium_entitlement_unlocks_paid_models_with_platform_key(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    paid = _add_model(client, admin, model_id="acme/pro", tier="paid")
    _set_platform_key(client, admin, "acme-llm")
    user_id, headers = make_user()
    client.put(f"/api/v1/admin/users/{user_id}/entitlements/premium_models", json={"enabled": True}, headers=admin)
    entry = next(m for m in client.get("/api/v1/models", headers=headers).json()["models"] if m["id"] == paid["id"])
    assert entry["available"] is True


def test_free_only_blocks_paid_models_even_with_own_key(client, make_admin, make_user, fake_llm):
    _, admin = make_admin()
    paid = _add_model(client, admin, model_id="acme/pro", tier="paid")
    user_id, headers = make_user()
    client.post("/api/v1/keys", json={"scope": "acme-llm", "key": USER_KEY}, headers=headers)
    client.put("/api/v1/providers/settings", json={"free_only": True}, headers=headers)
    entry = next(m for m in client.get("/api/v1/models", headers=headers).json()["models"] if m["id"] == paid["id"])
    assert entry["available"] is False and "Free tools only" in entry["reason"]


# ---------------------------------------------------------------- keys

def test_keys_are_encrypted_masked_and_never_returned(client, make_admin, make_user):
    _, admin = make_admin()
    _, headers = make_user()
    created = client.post("/api/v1/keys", json={"scope": "Gemini", "key": USER_KEY, "label": "mine"},
                          headers=headers)
    platform = client.post("/api/v1/admin/keys", json={"scope": "gemini", "key": PLATFORM_KEY}, headers=admin)
    responses = [created, platform, client.get("/api/v1/keys", headers=headers),
                 client.get("/api/v1/admin/keys", headers=admin)]
    for resp in responses:
        assert resp.status_code in (200, 201)
        assert USER_KEY not in resp.text and PLATFORM_KEY not in resp.text
    assert created.json()["last4"] == USER_KEY[-4:] and created.json()["scope"] == "gemini"

    db = SessionLocal()
    try:
        for row in db.query(Secret).all():
            assert USER_KEY not in row.ciphertext and PLATFORM_KEY not in row.ciphertext
        audit_blob = json.dumps([[a.before, a.after] for a in db.query(AuditLog).all()])
        assert USER_KEY not in audit_blob and PLATFORM_KEY not in audit_blob
    finally:
        db.close()


def test_key_lookup_order_user_then_platform_then_env(make_user, monkeypatch):
    user_id, _ = make_user()
    db = SessionLocal()
    try:
        assert key_store.resolve(db, user_id=user_id, scope="gemini") is None
        monkeypatch.setattr(key_store.get_settings(), "GEMINI_API_KEY", "env-key-123456")
        assert key_store.resolve(db, user_id=user_id, scope="gemini").source == "env"
        key_store.save_key(db, owner_type="platform", user_id=None, scope="gemini", value=PLATFORM_KEY)
        assert key_store.resolve(db, user_id=user_id, scope="gemini").value == PLATFORM_KEY
        key_store.save_key(db, owner_type="user", user_id=user_id, scope="gemini", value=USER_KEY)
        resolved = key_store.resolve(db, user_id=user_id, scope="gemini")
        assert (resolved.value, resolved.source) == (USER_KEY, "byok")
        db.commit()
    finally:
        db.close()


def test_users_cannot_touch_each_others_keys(client, make_user):
    _, alice = make_user()
    _, bob = make_user()
    key_id = client.post("/api/v1/keys", json={"scope": "groq", "key": USER_KEY}, headers=alice).json()["id"]
    assert client.delete(f"/api/v1/keys/{key_id}", headers=bob).status_code == 404
    assert client.get("/api/v1/keys", headers=bob).json()["keys"] == []
    assert client.delete(f"/api/v1/keys/{key_id}", headers=alice).status_code == 204


def test_stored_keys_never_travel_in_celery_args(client, make_user, dispatched):
    _, headers = make_user()
    client.post("/api/v1/keys", json={"scope": "gemini", "key": USER_KEY}, headers=headers)
    body = {"resume_summary": "x", "skills": ["python"], "jobs": [{"title": "Dev", "company": "A"}]}
    assert client.post("/api/v1/score", json=body, headers=headers).status_code == 202
    assert USER_KEY not in json.dumps(dispatched[-1]["args"])


# ---------------------------------------------------------------- job sources

SEARCH = {"keywords": ["Python"], "locations": ["Pune"], "sources": ["linkedin", "naukri"],
          "enable_naukri_apify": True, "apify_api_token": "apify-token-123456789"}


def test_free_only_skips_paid_sources_and_says_why(client, make_user, dispatched):
    _, headers = make_user()
    assert client.post("/api/v1/search", json=SEARCH, headers=headers).status_code == 202
    params = dispatched[-1]["args"][0]
    assert params["enable_naukri_apify"] is True and params["sources"] == ["linkedin", "naukri"]

    client.put("/api/v1/providers/settings", json={"free_only": True}, headers=headers)
    assert client.post("/api/v1/search", json=SEARCH, headers=headers).status_code == 202
    params = dispatched[-1]["args"][0]
    assert params["enable_naukri_apify"] is False
    assert params["sources"] == ["linkedin", "naukri"]
    assert any("Free tools only" in note for note in params["skipped_sources"])

    sources = {s["key"]: s for s in client.get("/api/v1/providers", headers=headers).json()["sources"]}
    assert sources["naukri_apify"]["available"] is False and sources["linkedin"]["available"] is True


def test_user_and_admin_switches_remove_sources(client, make_admin, make_user, dispatched):
    _, admin = make_admin()
    _, headers = make_user()
    client.put("/api/v1/providers/settings", json={"enabled": {"linkedin": False}}, headers=headers)
    assert client.patch("/api/v1/admin/providers/naukri", json={"enabled_global": False},
                        headers=admin).status_code == 200
    body = {**SEARCH, "enable_naukri_apify": False}
    resp = client.post("/api/v1/search", json=body, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "no_sources_available"


def test_apify_without_any_key_is_skipped(client, make_user, dispatched):
    _, headers = make_user()
    body = {**SEARCH, "apify_api_token": None}
    assert client.post("/api/v1/search", json=body, headers=headers).status_code == 202
    params = dispatched[-1]["args"][0]
    assert params["enable_naukri_apify"] is False
    assert any("apify API key" in note for note in params["skipped_sources"])


# ---------------------------------------------------------------- admin rules

def test_only_admins_manage_models_keys_and_sources(client, make_user):
    _, headers = make_user()
    assert client.get("/api/v1/admin/models", headers=headers).status_code == 403
    assert client.post("/api/v1/admin/keys", json={"scope": "groq", "key": PLATFORM_KEY},
                       headers=headers).status_code == 403
    assert client.patch("/api/v1/admin/providers/linkedin", json={"enabled_global": False},
                        headers=headers).status_code == 403


def test_model_changes_are_audited(client, make_admin):
    admin_id, admin = make_admin()
    model = _add_model(client, admin)
    client.patch(f"/api/v1/admin/models/{model['id']}", json={"priority": 7}, headers=admin)
    client.delete(f"/api/v1/admin/models/{model['id']}", headers=admin)
    db = SessionLocal()
    try:
        actions = [a.action for a in db.query(AuditLog).filter(AuditLog.target_id == model["id"]).order_by(AuditLog.at)]
        assert actions == ["model.create", "model.update", "model.delete"]
    finally:
        db.close()


def test_openai_compatible_needs_a_clean_base_url(client, make_admin):
    _, admin = make_admin()
    body = {"provider": "openai_compatible", "model_id": "x", "display_name": "X"}
    assert client.post("/api/v1/admin/models", json=body, headers=admin).status_code == 422
    body["base_url"] = "https://user:pass@api.example.com/v1"
    assert client.post("/api/v1/admin/models", json=body, headers=admin).status_code == 422
    body["base_url"] = "ftp://api.example.com"
    assert client.post("/api/v1/admin/models", json=body, headers=admin).status_code == 422


def test_admin_test_button_reports_result(client, make_admin, fake_llm):
    _, admin = make_admin()
    model = _add_model(client, admin)
    resp = client.post(f"/api/v1/admin/models/{model['id']}/test", headers=admin).json()
    assert resp["ok"] is True and resp["key_source"] == "none"  # openai_compatible may run keyless
    fake_llm.fail_models.add("acme/fast-1")
    resp = client.post(f"/api/v1/admin/models/{model['id']}/test", headers=admin).json()
    assert resp["ok"] is False and "503" in resp["error"]


# ---------------------------------------------------------------- anthropic adapter

def test_anthropic_refusal_is_a_failed_call(monkeypatch):
    import anthropic
    from types import SimpleNamespace
    from app.providers.llm.base import ModelSpec

    class FakeMessages:
        def create(self, **kw):
            assert "temperature" not in kw and "thinking" not in kw
            return SimpleNamespace(stop_reason="refusal", content=[], usage=SimpleNamespace(input_tokens=5, output_tokens=0))

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=FakeMessages()))
    with pytest.raises(LLMError) as err:
        adapters.AnthropicAdapter().complete(ModelSpec("anthropic", "claude-opus-5-5"), api_key="k" * 20,
                                             system="s", user="u", json_mode=True, max_tokens=100, timeout=5)
    assert err.value.retryable is False
