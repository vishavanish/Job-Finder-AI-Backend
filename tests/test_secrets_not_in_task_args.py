import json

from app.core.config import get_settings
from app.services import secret_refs

GEMINI = "gemini-user-key-123456789"
GROQ = "groq-user-key-123456789"
HF = "hf-user-key-123456789"
APIFY = "apify-user-token-123456789"

SCORE_PROFILE = {
    "resume_summary": "Python backend engineer",
    "skills": ["python", "fastapi"],
    "gemini_api_key": GEMINI,
    "groq_api_key": GROQ,
    "groq_model": "custom/groq-model",
    "hf_api_key": HF,
}
SEARCH = {"keywords": ["Python"], "locations": ["Pune"], "sources": ["naukri"],
          "enable_naukri_apify": True, "apify_api_token": APIFY}


def _assert_no_plaintext(call: dict) -> None:
    blob = json.dumps(call["args"])
    for secret in (GEMINI, GROQ, HF, APIFY):
        assert secret not in blob


def test_score_args_carry_refs_not_keys(client, make_user, dispatched):
    _, headers = make_user()
    body = {**SCORE_PROFILE, "jobs": [{"title": "Dev", "company": "Acme", "description": "python"}]}
    assert client.post("/api/v1/score", json=body, headers=headers).status_code == 202

    _assert_no_plaintext(dispatched[-1])
    params = dispatched[-1]["args"][1]
    assert secret_refs.resolve(params["gemini_api_key_ref"]) == GEMINI
    assert secret_refs.resolve(params["groq_api_key_ref"]) == GROQ
    assert secret_refs.resolve(params["hf_api_key_ref"]) == HF
    assert params["groq_model"] == "custom/groq-model"  # 6.2 #3: Groq settings are forwarded now


def test_pipeline_args_carry_refs_not_keys(client, make_user, dispatched):
    _, headers = make_user()
    body = {"search": SEARCH, "score": SCORE_PROFILE}

    assert client.post("/api/v1/pipeline/run", json=body, headers=headers).status_code == 202
    assert client.post("/api/v1/pipeline/run/stream", json=body, headers=headers).status_code == 202

    for call in dispatched[-2:]:
        _assert_no_plaintext(call)
    search_params = dispatched[-1]["args"][1]
    assert secret_refs.resolve(search_params["apify_api_token_ref"]) == APIFY


def test_redis_holds_only_ciphertext(fake_redis):
    ref = secret_refs.stash(GEMINI)
    stored = fake_redis.get(f"secret_ref:{ref}")
    assert stored and GEMINI not in stored


def test_no_ref_falls_back_to_server_key():
    assert secret_refs.stash(None) == ""
    assert secret_refs.resolve("", fallback="server-key") == "server-key"


def test_expired_ref_fails_loudly(fake_redis):
    ref = secret_refs.stash(GEMINI)
    fake_redis.delete(f"secret_ref:{ref}")
    try:
        secret_refs.resolve(ref, fallback="server-key")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expired ref must not silently use the server key")


def test_refs_expire_with_task_results(fake_redis):
    ref = secret_refs.stash(GEMINI)
    ttl = fake_redis.ttl(f"secret_ref:{ref}")
    assert 0 < ttl <= get_settings().TASK_RESULT_TTL_SECONDS
