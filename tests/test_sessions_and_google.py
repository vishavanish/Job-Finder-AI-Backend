from datetime import datetime, timedelta, timezone

from app.api.routes import auth as auth_route
from app.core.auth import create_access_token
from app.core.config import get_settings
from app.providers import google_identity

COOKIE = get_settings().REFRESH_COOKIE_NAME


def _login(client, email="sess@example.com"):
    client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    assert resp.status_code == 200
    return resp


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_sets_httponly_refresh_cookie(client):
    resp = _login(client, "cookie@example.com")
    set_cookie = resp.headers["set-cookie"].lower()
    assert f"{COOKIE}=" in set_cookie
    assert "httponly" in set_cookie
    assert "path=/api/v1/auth" in set_cookie


def test_refresh_rotates_and_old_token_is_rejected(client):
    _login(client, "rotate@example.com")
    old = client.cookies.get(COOKIE)

    resp = client.post("/api/v1/auth/refresh")
    assert resp.status_code == 200
    new = client.cookies.get(COOKIE)
    assert new and new != old
    assert client.get("/api/v1/auth/me", headers=_bearer(resp.json()["access_token"])).status_code == 200

    client.cookies.set(COOKIE, old, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_token_reuse_revokes_every_session(client):
    first = _login(client, "reuse@example.com")
    stolen = client.cookies.get(COOKIE)
    rotated = client.post("/api/v1/auth/refresh")
    access_after_rotation = rotated.json()["access_token"]

    client.cookies.set(COOKIE, stolen, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401  # replay detected

    for token in (first.json()["access_token"], access_after_rotation):
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401


def test_logout_kills_the_refresh_token(client):
    _login(client, "logout@example.com")
    token = client.cookies.get(COOKIE)
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set(COOKIE, token, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_access_tokens_are_short_lived(client, make_user):
    user_id, _ = make_user()
    expired = create_access_token(user_id, expires_minutes=-1)
    assert client.get("/api/v1/auth/me", headers=_bearer(expired)).status_code == 401
    assert get_settings().ACCESS_TOKEN_EXPIRE_MINUTES == 15


def test_set_password_for_otp_user(client, make_user):
    _, headers = make_user()
    assert client.post("/api/v1/auth/password", json={"password": "a-new-password"}, headers=headers).status_code == 200


# ---- Google ----

def _fake_google(monkeypatch, **claims):
    base = {"iss": "accounts.google.com", "sub": "google-sub-1", "email": "g@example.com",
            "email_verified": True, "name": "G User"}
    base.update(claims)
    monkeypatch.setattr(google_identity, "verify_id_token", lambda token: base)


def test_google_sign_in_creates_verified_user(client, monkeypatch):
    _fake_google(monkeypatch, sub="sub-new", email="Google.New@Example.com")
    resp = client.post("/api/v1/auth/google", json={"id_token": "x" * 40})
    assert resp.status_code == 200
    user = resp.json()["user"]
    assert user["email"] == "google.new@example.com"
    assert user["auth_provider"] == "google"
    assert user["email_verified_at"] is not None


def test_google_links_to_existing_account_by_email(client, monkeypatch, make_user):
    client.post("/api/v1/auth/register", json={"email": "link@example.com", "password": "password123"})
    _fake_google(monkeypatch, sub="sub-link", email="link@example.com")
    resp = client.post("/api/v1/auth/google", json={"id_token": "x" * 40})
    assert resp.status_code == 200
    assert resp.json()["user"]["auth_provider"] == "password"  # same account, not a new one


def test_google_requires_verified_email(client, monkeypatch):
    _fake_google(monkeypatch, sub="sub-unverified", email="unverified@example.com", email_verified=False)
    assert client.post("/api/v1/auth/google", json={"id_token": "x" * 40}).status_code == 401


def test_google_rejects_invalid_token(client, monkeypatch):
    def bad(token):
        raise google_identity.GoogleTokenInvalid("wrong audience")
    monkeypatch.setattr(google_identity, "verify_id_token", bad)
    assert client.post("/api/v1/auth/google", json={"id_token": "x" * 40}).status_code == 401


def test_google_real_verifier_checks_audience(monkeypatch):
    """The provider passes our client ids as `aud` to google-auth."""
    seen = {}

    def fake_verify(token, request, audience=None):
        seen["audience"] = audience
        return {"iss": "accounts.google.com", "sub": "s"}

    monkeypatch.setattr(google_identity.id_token, "verify_oauth2_token", fake_verify)
    google_identity.verify_id_token("token")
    assert seen["audience"] == ["test-client-id.apps.googleusercontent.com"]


def test_google_wrong_issuer_rejected(monkeypatch):
    monkeypatch.setattr(google_identity.id_token, "verify_oauth2_token",
                        lambda token, request, audience=None: {"iss": "evil.example.com", "sub": "s"})
    try:
        google_identity.verify_id_token("token")
    except google_identity.GoogleTokenInvalid:
        pass
    else:
        raise AssertionError("issuer must be checked")
