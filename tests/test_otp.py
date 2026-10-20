"""OTP rules from CLAUDE.md 7.1 — each test names the rule it proves."""
import pytest

from app.api.routes import auth as auth_route
from app.core.config import get_settings
from app.services import otp

REQUEST = "/api/v1/auth/otp/request"
VERIFY = "/api/v1/auth/otp/verify"


class _Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, *, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})

    def code_for(self, email):
        mail = [m for m in self.sent if m["to"] == email][-1]
        return mail["subject"].rsplit(" ", 1)[-1]


@pytest.fixture
def outbox(monkeypatch):
    box = _Outbox()
    monkeypatch.setattr(auth_route, "get_email_sender", lambda: box)
    return box


def _set_invite_only(client, admin_headers, value: bool):
    assert client.put("/api/v1/admin/flags/invite_only", json={"value": value},
                      headers=admin_headers).status_code == 200


def test_code_signs_in_and_creates_verified_account(client, outbox):
    assert client.post(REQUEST, json={"email": "New.Person@Example.com"}).status_code == 202
    code = outbox.code_for("new.person@example.com")
    assert len(code) == 6 and code.isdigit()

    resp = client.post(VERIFY, json={"email": "new.person@example.com", "code": code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == "new.person@example.com"
    assert body["user"]["email_verified_at"] is not None
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}).status_code == 200


def test_code_is_single_use(client, outbox):
    client.post(REQUEST, json={"email": "once@example.com"})
    code = outbox.code_for("once@example.com")
    assert client.post(VERIFY, json={"email": "once@example.com", "code": code}).status_code == 200
    assert client.post(VERIFY, json={"email": "once@example.com", "code": code}).status_code == 400


def test_five_wrong_attempts_invalidate_the_code(client, outbox):
    client.post(REQUEST, json={"email": "guess@example.com"})
    code = outbox.code_for("guess@example.com")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(get_settings().OTP_MAX_ATTEMPTS):
        assert client.post(VERIFY, json={"email": "guess@example.com", "code": wrong}).status_code == 400
    assert client.post(VERIFY, json={"email": "guess@example.com", "code": code}).status_code == 400


def test_resend_cooldown(client, outbox):
    assert client.post(REQUEST, json={"email": "cool@example.com"}).status_code == 202
    resp = client.post(REQUEST, json={"email": "cool@example.com"})
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


def test_hourly_cap_per_email(client, outbox, fake_redis):
    email = "cap@example.com"
    for _ in range(get_settings().OTP_MAX_REQUESTS_PER_HOUR):
        assert client.post(REQUEST, json={"email": email}).status_code == 202
        fake_redis.delete(f"otp_cooldown:{otp._email_id(email)}")
    assert client.post(REQUEST, json={"email": email}).status_code == 429


def test_code_expires(client, outbox, fake_redis):
    client.post(REQUEST, json={"email": "late@example.com"})
    code = outbox.code_for("late@example.com")
    ttl = fake_redis.ttl(f"otp:{otp._email_id('late@example.com')}")
    assert 0 < ttl <= get_settings().OTP_TTL_SECONDS
    fake_redis.delete(f"otp:{otp._email_id('late@example.com')}")  # what expiry does
    assert client.post(VERIFY, json={"email": "late@example.com", "code": code}).status_code == 400


def test_redis_never_holds_the_code_or_the_email(client, outbox, fake_redis):
    client.post(REQUEST, json={"email": "private@example.com"})
    code = outbox.code_for("private@example.com")
    dump = " ".join(
        f"{k} {fake_redis.hgetall(k) if fake_redis.type(k) == 'hash' else fake_redis.get(k)}"
        for k in fake_redis.keys("otp*")
    )
    assert code not in dump
    assert "private@example.com" not in dump


def test_same_response_whether_or_not_the_account_can_sign_in(client, outbox, make_admin):
    _, admin = make_admin()
    _set_invite_only(client, admin, True)

    known = client.post(REQUEST, json={"email": "nobody-known@example.com"})
    assert known.status_code == 202
    assert known.json() == {"detail": auth_route.OTP_SENT_MESSAGE}
    assert outbox.sent == []  # uninvited address: nothing is emailed


def test_invite_only_blocks_unknown_emails_even_with_a_valid_code(client, outbox, make_admin, monkeypatch):
    _, admin = make_admin()
    _set_invite_only(client, admin, True)
    monkeypatch.setattr(otp.secrets, "randbelow", lambda n: 123456)

    client.post(REQUEST, json={"email": "stranger@example.com"})
    assert client.post(VERIFY, json={"email": "stranger@example.com", "code": "123456"}).status_code == 403


def test_invited_user_can_sign_in_in_invite_only_mode(client, outbox, make_admin):
    _, admin = make_admin()
    _set_invite_only(client, admin, True)
    assert client.post("/api/v1/admin/users", json={"email": "invited@example.com"}, headers=admin).status_code == 201

    client.post(REQUEST, json={"email": "invited@example.com"})
    code = outbox.code_for("invited@example.com")
    assert client.post(VERIFY, json={"email": "invited@example.com", "code": code}).status_code == 200


def test_disabled_account_cannot_sign_in_with_a_code(client, outbox, make_admin, make_user, monkeypatch):
    _, admin = make_admin()
    user_id, _ = make_user()
    email = client.get(f"/api/v1/admin/users/{user_id}", headers=admin).json()["email"]
    client.post(f"/api/v1/admin/users/{user_id}/disable", headers=admin)
    monkeypatch.setattr(otp.secrets, "randbelow", lambda n: 654321)

    client.post(REQUEST, json={"email": email})
    assert outbox.sent == []
    assert client.post(VERIFY, json={"email": email, "code": "654321"}).status_code == 403


def test_console_email_is_refused_in_production(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production")
    assert client.post(REQUEST, json={"email": "prod@example.com"}).status_code == 503


def test_malformed_code_is_rejected_by_validation(client):
    assert client.post(VERIFY, json={"email": "x@example.com", "code": "12ab56"}).status_code == 422


def test_verify_errors_carry_codes_for_the_ui(client, outbox):
    """The sign-in screen needs to tell wrong / locked / expired apart."""
    max_attempts = get_settings().OTP_MAX_ATTEMPTS
    client.post(REQUEST, json={"email": "ui@example.com"})
    code = outbox.code_for("ui@example.com")
    wrong = "000000" if code != "000000" else "111111"

    first = client.post(VERIFY, json={"email": "ui@example.com", "code": wrong}).json()["detail"]
    assert first["code"] == "otp_invalid" and first["attempts_left"] == max_attempts - 1

    for _ in range(max_attempts - 2):
        client.post(VERIFY, json={"email": "ui@example.com", "code": wrong})
    last = client.post(VERIFY, json={"email": "ui@example.com", "code": wrong}).json()["detail"]
    assert last["code"] == "otp_locked" and last["attempts_left"] == 0

    gone = client.post(VERIFY, json={"email": "ui@example.com", "code": code}).json()["detail"]
    assert gone["code"] == "otp_expired"


def test_cooldown_error_has_code_and_retry_after(client, outbox):
    client.post(REQUEST, json={"email": "wait@example.com"})
    detail = client.post(REQUEST, json={"email": "wait@example.com"}).json()["detail"]
    assert detail["code"] == "otp_cooldown" and detail["retry_after"] > 0
