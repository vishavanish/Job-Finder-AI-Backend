"""P1 acceptance: disabled user locked out immediately; RBAC enforced
server-side; every admin action audited; entitlements gate features."""
from datetime import datetime, timedelta, timezone

from app.core.db import SessionLocal
from app.models.admin import AuditLog
from app.services import pipeline_progress
from tests.conftest import set_role

SEARCH = {"keywords": ["Python"], "locations": ["Pune"], "sources": ["linkedin"]}
APPLY = {"jobs": [{"title": "Dev", "company": "Acme", "url": "https://x.example/1"}],
         "applicant_info": {"first_name": "A", "last_name": "B", "email": "a@b.co"}}
COOKIE = "jf_refresh"


def _audit(target_id=None, action=None):
    db = SessionLocal()
    try:
        q = db.query(AuditLog)
        if target_id:
            q = q.filter(AuditLog.target_id == target_id)
        if action:
            q = q.filter(AuditLog.action == action)
        return q.all()
    finally:
        db.close()


# ---- RBAC ----

def test_regular_users_cannot_reach_admin_routes(client, make_user):
    _, headers = make_user()
    for method, path in (("GET", "/api/v1/admin/users"), ("POST", "/api/v1/admin/users"),
                         ("GET", "/api/v1/admin/flags"), ("GET", "/api/v1/admin/audit")):
        assert client.request(method, path, json={"email": "z@example.com"}, headers=headers).status_code == 403


def test_managers_cannot_use_admin_only_routes(client, make_user):
    manager_id, manager = make_user()
    set_role(manager_id, "manager")
    assert client.post("/api/v1/admin/users", json={"email": "m@example.com"}, headers=manager).status_code == 403
    assert client.get("/api/v1/admin/audit", headers=manager).status_code == 403
    assert client.put("/api/v1/admin/flags/invite_only", json={"value": True}, headers=manager).status_code == 403


# ---- disable / enable ----

def test_disabled_user_is_locked_out_immediately(client, make_admin, make_user, dispatched, revoked_tasks):
    _, admin = make_admin()
    user_id, user = make_user()
    refresh_cookie = client.cookies.get(COOKIE)

    task_id = client.post("/api/v1/search", json=SEARCH, headers=user).json()["task_id"]
    run_id = pipeline_progress.create_run(user_id=user_id, total_keywords=1)

    resp = client.post(f"/api/v1/admin/users/{user_id}/disable", headers=admin)
    assert resp.status_code == 200 and resp.json()["is_active"] is False

    me = client.get("/api/v1/auth/me", headers=user)                                # access token dead
    assert me.status_code == 403 and me.json()["detail"]["code"] == "account_disabled"
    client.cookies.set(COOKIE, refresh_cookie, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 403                  # refresh dead
    assert task_id in revoked_tasks                                                # running work cancelled
    assert pipeline_progress.get_run_meta(run_id=run_id)["status"] == "cancelled"

    [entry] = _audit(target_id=user_id, action="user.disable")
    assert entry.before["is_active"] is True and entry.after["is_active"] is False


def test_re_enabled_user_must_sign_in_again(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, old_headers = make_user()
    email = client.get(f"/api/v1/admin/users/{user_id}", headers=admin).json()["email"]
    client.post(f"/api/v1/admin/users/{user_id}/disable", headers=admin)
    client.post(f"/api/v1/admin/users/{user_id}/enable", headers=admin)

    assert client.get("/api/v1/auth/me", headers=old_headers).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": "password123"}).status_code == 200


def test_admin_cannot_disable_or_demote_themselves(client, make_admin):
    admin_id, admin = make_admin()
    assert client.post(f"/api/v1/admin/users/{admin_id}/disable", headers=admin).status_code == 400
    assert client.patch(f"/api/v1/admin/users/{admin_id}", json={"role": "user"}, headers=admin).status_code == 400


def test_revoke_sessions_signs_user_out_everywhere(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, user = make_user()
    assert client.post(f"/api/v1/admin/users/{user_id}/revoke-sessions", headers=admin).status_code == 204
    assert client.get("/api/v1/auth/me", headers=user).status_code == 401
    assert _audit(target_id=user_id, action="user.revoke_sessions")


# ---- users CRUD ----

def test_admin_creates_lists_and_updates_users(client, make_admin, make_user):
    admin_id, admin = make_admin()
    manager_id, _ = make_user()
    set_role(manager_id, "manager")

    created = client.post("/api/v1/admin/users", json={"email": "Team.Member@example.com", "manager_id": manager_id},
                          headers=admin)
    assert created.status_code == 201
    new_id = created.json()["id"]
    assert created.json()["email"] == "team.member@example.com"

    found = client.get("/api/v1/admin/users", params={"q": "team.member"}, headers=admin).json()
    assert [u["id"] for u in found["users"]] == [new_id]

    patched = client.patch(f"/api/v1/admin/users/{new_id}", json={"role": "manager", "manager_id": None}, headers=admin)
    assert patched.status_code == 200
    assert patched.json()["role"] == "manager" and patched.json()["manager_id"] is None

    assert client.post("/api/v1/admin/users", json={"email": "team.member@example.com"}, headers=admin).status_code == 409
    assert {e.action for e in _audit(target_id=new_id)} == {"user.create", "user.update"}
    assert all(e.actor_id == admin_id for e in _audit(target_id=new_id))


def test_manager_id_must_point_at_a_manager(client, make_admin, make_user):
    _, admin = make_admin()
    plain_id, _ = make_user()
    resp = client.post("/api/v1/admin/users", json={"email": "bad-mgr@example.com", "manager_id": plain_id}, headers=admin)
    assert resp.status_code == 400


# ---- managers ----

def test_manager_sees_and_manages_only_their_team(client, make_user):
    manager_id, manager = make_user()
    set_role(manager_id, "manager")
    member_id, member = make_user()
    set_role(member_id, "user", manager_id=manager_id)
    outsider_id, _ = make_user()

    listed = client.get("/api/v1/admin/users", headers=manager).json()
    assert [u["id"] for u in listed["users"]] == [member_id]
    assert client.get(f"/api/v1/admin/users/{outsider_id}", headers=manager).status_code == 404
    assert client.post(f"/api/v1/admin/users/{outsider_id}/disable", headers=manager).status_code == 404

    assert client.post(f"/api/v1/admin/users/{member_id}/disable", headers=manager).status_code == 200
    assert client.get("/api/v1/auth/me", headers=member).status_code == 403
    assert _audit(target_id=member_id, action="user.disable")[0].actor_id == manager_id


def test_manager_cannot_grant_more_than_they_have(client, make_admin, make_user):
    _, admin = make_admin()
    manager_id, manager = make_user()
    set_role(manager_id, "manager")
    member_id, _ = make_user()
    set_role(member_id, "user", manager_id=manager_id)

    # live_copilot is off by default for everyone, including the manager
    resp = client.put(f"/api/v1/admin/users/{member_id}/entitlements/live_copilot", json={"enabled": True}, headers=manager)
    assert resp.status_code == 403

    client.put(f"/api/v1/admin/users/{manager_id}/entitlements/job_search",
               json={"enabled": True, "quota_per_day": 10}, headers=admin)
    too_much = client.put(f"/api/v1/admin/users/{member_id}/entitlements/job_search",
                          json={"enabled": True, "quota_per_day": 50}, headers=manager)
    assert too_much.status_code == 403
    within = client.put(f"/api/v1/admin/users/{member_id}/entitlements/job_search",
                        json={"enabled": True, "quota_per_day": 5}, headers=manager)
    assert within.status_code == 200 and within.json()["quota_per_day"] == 5


# ---- entitlements ----

def test_disabled_feature_blocks_the_route_with_a_reason(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, user = make_user()
    client.put(f"/api/v1/admin/users/{user_id}/entitlements/job_search", json={"enabled": False}, headers=admin)

    resp = client.post("/api/v1/search", json=SEARCH, headers=user)
    assert resp.status_code == 403
    assert "job search" in resp.json()["detail"]
    assert client.post("/api/v1/pipeline/run/stream", json={"search": SEARCH, "score": {
        "resume_summary": "x", "skills": ["python"]}}, headers=user).status_code == 403


def test_daily_quota_is_enforced(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, user = make_user()
    client.put(f"/api/v1/admin/users/{user_id}/entitlements/job_search",
               json={"enabled": True, "quota_per_day": 1}, headers=admin)

    assert client.post("/api/v1/search", json=SEARCH, headers=user).status_code == 202
    assert client.post("/api/v1/search", json=SEARCH, headers=user).status_code == 429
    mine = {e["feature"]: e for e in client.get("/api/v1/auth/me/entitlements", headers=user).json()}
    assert mine["job_search"]["used_today"] == 2


def test_expired_override_falls_back_to_default(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, user = make_user()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    client.put(f"/api/v1/admin/users/{user_id}/entitlements/job_search",
               json={"enabled": False, "expires_at": past}, headers=admin)
    assert client.post("/api/v1/search", json=SEARCH, headers=user).status_code == 202


def test_reset_entitlement_restores_default(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, user = make_user()
    client.put(f"/api/v1/admin/users/{user_id}/entitlements/job_search", json={"enabled": False}, headers=admin)
    resp = client.delete(f"/api/v1/admin/users/{user_id}/entitlements/job_search", headers=admin)
    assert resp.json()["source"] == "default" and resp.json()["enabled"] is True
    assert {e.action for e in _audit(target_id=user_id)} >= {"entitlement.set", "entitlement.reset"}


def test_me_includes_entitlements_with_quota_remaining(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, user = make_user()
    client.put(f"/api/v1/admin/users/{user_id}/entitlements/job_search",
               json={"enabled": True, "quota_per_day": 3}, headers=admin)
    client.post("/api/v1/search", json=SEARCH, headers=user)

    me = client.get("/api/v1/auth/me", headers=user).json()
    assert me["entitlements"]["job_search"] == {"enabled": True, "max_level": None,
                                                "quota_per_day": 3, "quota_remaining": 2}
    assert me["entitlements"]["live_copilot"]["enabled"] is False


def test_live_copilot_is_off_by_default(client, make_user):
    _, user = make_user()
    mine = {e["feature"]: e for e in client.get("/api/v1/auth/me/entitlements", headers=user).json()}
    assert mine["live_copilot"]["enabled"] is False
    assert mine["auto_apply"]["max_level"] == 1


# ---- flags ----

def test_auto_apply_kill_switch(client, make_admin, make_user):
    _, admin = make_admin()
    _, user = make_user()
    assert client.put("/api/v1/admin/flags/auto_apply_enabled", json={"value": False}, headers=admin).status_code == 200
    assert client.post("/api/v1/apply", json=APPLY, headers=user).status_code == 503
    assert _audit(target_id="auto_apply_enabled", action="flag.set")


def test_invite_only_closes_password_signup(client, make_admin):
    _, admin = make_admin()
    client.put("/api/v1/admin/flags/invite_only", json={"value": True}, headers=admin)
    resp = client.post("/api/v1/auth/register", json={"email": "closed@example.com", "password": "password123"})
    assert resp.status_code == 403


def test_flag_values_are_type_checked(client, make_admin):
    _, admin = make_admin()
    assert client.put("/api/v1/admin/flags/invite_only", json={"value": "yes"}, headers=admin).status_code == 422
    assert client.put("/api/v1/admin/flags/nope", json={"value": True}, headers=admin).status_code == 404


# ---- audit log ----

def test_audit_log_is_queryable_by_admin(client, make_admin, make_user):
    _, admin = make_admin()
    user_id, _ = make_user()
    client.post(f"/api/v1/admin/users/{user_id}/disable", headers=admin)
    entries = client.get("/api/v1/admin/audit", params={"target_id": user_id}, headers=admin).json()["entries"]
    assert "user.disable" in {e["action"] for e in entries}
    assert entries[0]["ip"]  # request ip captured


def test_bootstrap_script_creates_first_admin():
    from app.scripts.set_role import main
    assert main(["first.admin@example.com", "admin"]) == 0
    [entry] = _audit(action="user.role_set_cli")
    assert entry.actor_id is None and entry.after["role"] == "admin"
