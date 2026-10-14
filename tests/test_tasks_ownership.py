import pytest

from app.api.routes import tasks as tasks_route
from app.core.celery_app import celery_app

SEARCH_BODY = {"keywords": ["Python"], "locations": ["Pune"], "sources": ["linkedin"]}


class _FakeResult:
    state = "SUCCESS"
    result = {"jobs": [], "total_raw": 0, "total_usable": 0}
    info = None


@pytest.fixture(autouse=True)
def _no_result_backend(monkeypatch):
    monkeypatch.setattr(tasks_route, "AsyncResult", lambda task_id, app=None: _FakeResult())
    revoked = []
    monkeypatch.setattr(celery_app.control, "revoke", lambda task_id, **kw: revoked.append(task_id))
    return revoked


def _start_search(client, headers) -> str:
    resp = client.post("/api/v1/search", json=SEARCH_BODY, headers=headers)
    assert resp.status_code == 202, resp.text
    return resp.json()["task_id"]


def test_owner_can_read_their_task(client, make_user, dispatched):
    _, headers = make_user()
    task_id = _start_search(client, headers)

    assert dispatched[-1]["options"]["task_id"] == task_id
    resp = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_other_user_gets_404(client, make_user):
    _, owner = make_user()
    _, stranger = make_user()
    task_id = _start_search(client, owner)

    assert client.get(f"/api/v1/tasks/{task_id}", headers=stranger).status_code == 404


def test_unknown_task_id_is_404_not_pending(client, make_user):
    _, headers = make_user()
    resp = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000", headers=headers)
    assert resp.status_code == 404


def test_task_endpoint_requires_login(client, make_user):
    _, headers = make_user()
    task_id = _start_search(client, headers)
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 401


def test_only_owner_can_cancel(client, make_user, _no_result_backend):
    _, owner = make_user()
    _, stranger = make_user()
    task_id = _start_search(client, owner)

    assert client.post(f"/api/v1/tasks/{task_id}/cancel", headers=stranger).status_code == 404
    assert _no_result_backend == []

    assert client.post(f"/api/v1/tasks/{task_id}/cancel", headers=owner).status_code == 200
    assert _no_result_backend == [task_id]
