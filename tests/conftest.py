"""
Test harness: a throwaway SQLite database built by the real Alembic
migrations (so migrations are exercised too), fakeredis in place of
Upstash, and Celery publishing replaced by a recorder — no network,
no broker, no LLM calls.

Run from job_finder_api/:  python -m pytest
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

# Must be set before anything imports app.core.config (settings are
# lru_cached at first import). Explicit empty values override the real
# keys in job_finder_api/.env.
_TMP = Path(tempfile.mkdtemp(prefix="job_finder_tests_"))
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "REDIS_URL": "redis://localhost:6379/15",
    "JWT_SECRET_KEY": "test-jwt-secret",
    "SECRETS_MASTER_KEY": Fernet.generate_key().decode(),
    "ALLOW_SELF_REGISTRATION": "true",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "HF_API_KEY": "",
    "APIFY_API_TOKEN": "",
    "API_KEYS": "",
    "LOG_DIR": str(_TMP / "logs"),
    "OUTPUT_DIR": str(_TMP / "output"),
})

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from celery.app.task import Task  # noqa: E402

API_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _migrated_db():
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def fake_redis():
    from app.core import redis_client
    client = fakeredis.FakeRedis(decode_responses=True)
    redis_client.set_redis(client)
    yield client
    redis_client.set_redis(None)


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):
    """Every Celery publish is recorded here instead of sent to a broker."""
    calls: list[dict] = []

    def fake_apply_async(self, args=None, kwargs=None, **options):
        calls.append({"task": self.name, "args": args, "options": options})

    monkeypatch.setattr(Task, "apply_async", fake_apply_async)
    return calls


@pytest.fixture(scope="session")
def app():
    from app.main import app as fastapi_app
    from app.core.rate_limit import limiter
    limiter.enabled = False
    return fastapi_app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


_user_counter = 0


@pytest.fixture
def make_user(client):
    """Registers a fresh user and returns (user_id, auth headers)."""
    def _make() -> tuple[str, dict]:
        global _user_counter
        _user_counter += 1
        resp = client.post("/api/v1/auth/register", json={
            "email": f"user{_user_counter}@example.com", "password": "password123",
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}
    return _make
