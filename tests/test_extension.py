"""Browser extension "Job Finder capture" (P8a E1): pairing, capture-only tokens, saving jobs, the search source."""
from __future__ import annotations

from app.core.db import SessionLocal
from app.models.extension import CapturedJob, ExtensionToken
from app.services import job_sources

API = "/api/v1"
NAUKRI_JOBS = [
    {"title": "Python Developer", "company": "Orbit Labs", "location": "Pune",
     "url": "https://www.naukri.com/job-listings-python-developer-orbit-labs-pune-3-to-5-years-201024000001"},
    {"title": "Data Engineer <b>Urgent</b>", "company": "Kite", "location": "Mumbai",
     "url": "https://www.naukri.com/job-listings-data-engineer-kite-mumbai-201024000002"},
    {"title": "Phishing", "url": "https://evil.example/naukri.com/job"},             # not on the site
    {"title": "Insecure", "url": "http://www.naukri.com/job-listings-x-1"},          # not https
]


def _pair(client, headers) -> str:
    code = client.post(f"{API}/extension/pair", headers=headers).json()["code"]
    r = client.post(f"{API}/extension/token", json={"code": code, "name": "Chrome on laptop"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_pairing_gives_a_capture_only_token_once(client, make_user):
    _, headers = make_user()
    code = client.post(f"{API}/extension/pair", headers=headers).json()["code"]
    assert len(code) == 6 and code.isdigit()
    first = client.post(f"{API}/extension/token", json={"code": code})
    assert first.status_code == 200 and first.json()["token"].startswith("jfx_")
    again = client.post(f"{API}/extension/token", json={"code": code})  # single use
    assert again.status_code == 400 and again.json()["detail"]["code"] == "pair_code_invalid"
    token = first.json()["token"]
    db = SessionLocal()
    try:
        assert db.query(ExtensionToken).filter(ExtensionToken.token_hash == token).first() is None  # only the hash
    finally:
        db.close()
    ext = {"Authorization": f"Bearer {token}"}
    assert client.get(f"{API}/extension/me", headers=ext).status_code == 200
    assert client.get(f"{API}/profile", headers=ext).status_code == 401  # never a session token


def test_captures_keep_only_jobs_on_the_site_and_clean_the_text(client, make_user):
    _, headers = make_user()
    ext = {"Authorization": f"Bearer {_pair(client, headers)}"}
    body = {"site": "naukri", "page_url": "https://www.naukri.com/python-developer-jobs-in-pune", "jobs": NAUKRI_JOBS}
    r = client.post(f"{API}/extension/captures", headers=ext, json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"saved": 2, "updated": 0, "skipped": 2, "total": 2}
    again = client.post(f"{API}/extension/captures", headers=ext, json=body).json()
    assert again["saved"] == 0 and again["updated"] == 2 and again["total"] == 2  # same links -> updated
    db = SessionLocal()
    try:
        titles = {j.title: j for j in db.query(CapturedJob).all() if j.company in ("Orbit Labs", "Kite")}
    finally:
        db.close()
    assert "Data Engineer Urgent" in titles and titles["Data Engineer Urgent"].source == "Naukri"

    wrong = dict(body, page_url="https://evil.example/naukri")
    assert client.post(f"{API}/extension/captures", headers=ext, json=wrong).json()["detail"]["code"] == \
        "page_not_on_site"
    other = dict(body, site="timesjobs")
    assert client.post(f"{API}/extension/captures", headers=ext, json=other).json()["detail"]["code"] == \
        "site_unsupported"


def test_disconnect_and_disabled_feature_stop_the_extension(client, make_user, make_admin):
    user_id, headers = make_user()
    token = _pair(client, headers)
    ext = {"Authorization": f"Bearer {token}"}
    conns = client.get(f"{API}/extension/connections", headers=headers).json()
    assert len(conns["connections"]) == 1 and conns["connections"][0]["name"] == "Chrome on laptop"
    _, admin = make_admin()
    client.put(f"{API}/admin/users/{user_id}/entitlements/browser_capture", headers=admin, json={"enabled": False})
    assert client.get(f"{API}/extension/me", headers=ext).status_code == 403
    client.put(f"{API}/admin/users/{user_id}/entitlements/browser_capture", headers=admin, json={"enabled": True})
    assert client.get(f"{API}/extension/me", headers=ext).status_code == 200
    assert client.delete(f"{API}/extension/connections/{conns['connections'][0]['id']}",
                         headers=headers).status_code == 204
    assert client.get(f"{API}/extension/me", headers=ext).status_code == 401


def test_saved_jobs_become_a_search_source_once_connected(client, make_user):
    _, headers = make_user()
    view = {s["key"]: s for s in client.get(f"{API}/providers", headers=headers).json()["sources"]}
    assert view["browser_capture"]["available"] is False
    assert "connect the browser extension" in view["browser_capture"]["reason"]
    ext = {"Authorization": f"Bearer {_pair(client, headers)}"}
    view = {s["key"]: s for s in client.get(f"{API}/providers", headers=headers).json()["sources"]}
    assert view["browser_capture"]["available"] is True
    client.post(f"{API}/extension/captures", headers=ext,
                json={"site": "naukri", "page_url": "https://www.naukri.com/jobs", "jobs": NAUKRI_JOBS})
    from app.services import extension
    db = SessionLocal()
    try:
        user_id = db.query(ExtensionToken).order_by(ExtensionToken.created_at.desc()).first().user_id
        saved = extension.recent_captured_jobs(db, user_id, 24)
    finally:
        db.close()
    found = job_sources.scrape_all(["Python Developer"], ["Pune"], ["browser_capture"], captured_jobs=saved,
                                   max_age_hours=72)
    assert [j["title"] for j in found] == ["Python Developer"] and found[0]["source"] == "Naukri"
