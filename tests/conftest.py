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
    "REFRESH_COOKIE_SECURE": "false",  # TestClient talks plain http
    "EMAIL_BACKEND": "console",
    "GOOGLE_CLIENT_IDS": "test-client-id.apps.googleusercontent.com",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "HF_API_KEY": "",
    "APIFY_API_TOKEN": "",
    "ADZUNA_API_KEY": "",
    "JOOBLE_API_KEY": "",
    "CAREERJET_API_KEY": "",
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
        calls.append({"task": self.name, "args": args, "kwargs": kwargs, "options": options})

    monkeypatch.setattr(Task, "apply_async", fake_apply_async)
    return calls


@pytest.fixture(autouse=True)
def revoked_tasks(monkeypatch):
    """Celery revoke() needs a broker; record the ids instead."""
    from app.core.celery_app import celery_app
    revoked: list[str] = []
    monkeypatch.setattr(celery_app.control, "revoke", lambda task_id, **kw: revoked.append(task_id))
    return revoked


@pytest.fixture(autouse=True)
def _reset_feature_flags():
    yield
    from app.core.db import SessionLocal
    from app.models.admin import FeatureFlag
    db = SessionLocal()
    try:
        db.query(FeatureFlag).delete()
        db.commit()
    finally:
        db.close()


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


def _next_email() -> str:
    global _user_counter
    _user_counter += 1
    return f"user{_user_counter}@example.com"


@pytest.fixture
def make_user(client):
    """Registers a fresh user and returns (user_id, auth headers)."""
    def _make() -> tuple[str, dict]:
        resp = client.post("/api/v1/auth/register", json={"email": _next_email(), "password": "password123"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}
    return _make


def set_role(user_id: str, role: str, manager_id: str | None = None) -> None:
    from app.core.db import SessionLocal
    from app.models.user import User
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        user.role = role
        if manager_id is not None:
            user.manager_id = manager_id
        db.commit()
    finally:
        db.close()


@pytest.fixture
def make_admin(make_user):
    def _make() -> tuple[str, dict]:
        user_id, headers = make_user()
        set_role(user_id, "admin")
        return user_id, headers
    return _make


@pytest.fixture(autouse=True)
def _socket_exit_waits_for_the_app(monkeypatch):
    """Starlette's WebSocketTestSession cancels the app right after sending the
    disconnect, so a voice session still handling the drop could be cancelled
    mid-way and the test failed with CancelledError (only on slower CI runners).
    Send the disconnect first and wait (≤ 10 s) for the app to return."""
    import threading

    from starlette.testclient import WebSocketTestSession

    enter, exit_ = WebSocketTestSession.__enter__, WebSocketTestSession.__exit__

    def _enter(self):
        app, done = self.app, threading.Event()

        async def wrapped(scope, receive, send):
            try:
                await app(scope, receive, send)
            finally:
                done.set()

        self.app, self._app_done = wrapped, done
        return enter(self)

    def _exit(self, *args):
        done = getattr(self, "_app_done", None)
        if done is not None and not done.is_set():
            try:
                self.close(1000)
            except Exception:  # noqa: BLE001 — the normal exit below reports real errors
                pass
            done.wait(10)
        return exit_(self, *args)

    monkeypatch.setattr(WebSocketTestSession, "__enter__", _enter)
    monkeypatch.setattr(WebSocketTestSession, "__exit__", _exit)
