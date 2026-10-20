"""ALWAYS_ADMIN_EMAILS: owner accounts are admin on sign-up and every request,
get in while sign-up is invite-only, and can't be demoted or disabled."""
from __future__ import annotations

import itertools

import pytest

from app.core.config import get_settings
from tests.conftest import set_role

_n = itertools.count(1)


@pytest.fixture
def owner_email(monkeypatch):
    owner = f"owner{next(_n)}.always@example.com"
    monkeypatch.setattr(get_settings(), "ALWAYS_ADMIN_EMAILS", f"someone@else.test, {owner.upper()}")
    return owner


def _register(client, email):
    resp = client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


def test_owner_signs_up_as_admin(client, owner_email):
    user_id, headers = _register(client, owner_email)
    me = client.get("/api/v1/auth/me", headers=headers).json()
    assert me["role"] == "admin"


def test_owner_is_restored_to_admin_on_the_next_request(client, owner_email):
    user_id, headers = _register(client, owner_email)
    set_role(user_id, "user")  # e.g. a direct database edit
    assert client.get("/api/v1/admin/users", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "admin"


def test_owner_cannot_be_demoted_or_disabled(client, owner_email, make_admin):
    owner_id, _ = _register(client, owner_email)
    _, admin = make_admin()
    demote = client.patch(f"/api/v1/admin/users/{owner_id}", json={"role": "user"}, headers=admin)
    assert demote.status_code == 400 and "owner account" in demote.json()["detail"]
    disable = client.post(f"/api/v1/admin/users/{owner_id}/disable", headers=admin)
    assert disable.status_code == 400 and "owner account" in disable.json()["detail"]


def test_owner_can_register_while_invite_only(client, owner_email, make_admin):
    _, admin = make_admin()
    client.put("/api/v1/admin/flags/invite_only", json={"value": True}, headers=admin)
    try:
        assert client.post("/api/v1/auth/register",
                           json={"email": "stranger@example.com", "password": "password123"}).status_code == 403
        _register(client, owner_email)
    finally:
        client.put("/api/v1/admin/flags/invite_only", json={"value": False}, headers=admin)


def test_other_users_are_unaffected(client, owner_email, make_user):
    _, headers = make_user()
    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "user"
