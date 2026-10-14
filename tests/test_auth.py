from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.user import User


def test_register_then_login(client):
    body = {"email": "Login.Test@Example.com", "password": "password123"}
    assert client.post("/api/v1/auth/register", json=body).status_code == 201

    resp = client.post("/api/v1/auth/login", json={"email": "login.test@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "login.test@example.com"


def test_wrong_password_is_401(client, make_user):
    make_user()
    resp = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "nope-nope"})
    assert resp.status_code == 401


def test_registration_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "ALLOW_SELF_REGISTRATION", False)
    resp = client.post("/api/v1/auth/register", json={"email": "blocked@example.com", "password": "password123"})
    assert resp.status_code == 403


def test_disabled_user_is_locked_out_immediately(client, make_user):
    user_id, headers = make_user()
    assert client.get("/api/v1/applications", headers=headers).status_code == 200

    db = SessionLocal()
    try:
        db.query(User).filter(User.id == user_id).update({"is_active": False})
        db.commit()
    finally:
        db.close()

    assert client.get("/api/v1/applications", headers=headers).status_code == 401


def test_protected_routes_reject_missing_token(client):
    for method, path in (("post", "/api/v1/search"), ("post", "/api/v1/score"), ("post", "/api/v1/apply"),
                         ("post", "/api/v1/pipeline/run"), ("post", "/api/v1/company-ats/fetch"),
                         ("get", "/api/v1/applications")):
        assert client.request(method.upper(), path, json={}).status_code == 401, path
