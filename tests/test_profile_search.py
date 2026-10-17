"""P3 — Profile & Search: canonical jobs (LinkedIn wins across keyword
tasks), seen jobs not re-shown, filters, search profiles, preferences,
resume upload/versions/parse."""
from __future__ import annotations

import io

import pytest

from app.core import celery_tasks
from app.core.db import SessionLocal
from app.models.jobs import Job, JobSourceSeen, UserJob
from app.services import job_store, pipeline_progress, resume_store, search_profiles

API = "/api/v1"

INDEED = {"source": "Indeed", "title": "AI Engineer", "company": "ORBIT LABS", "location": "Pune",
          "url": "https://indeed.example/1", "description": "python llm fastapi " * 20}
LINKEDIN = {"source": "LinkedIn", "title": "AI Engineer", "company": "Orbit Labs Pvt. Ltd.",
            "location": "Pune, Maharashtra, India", "url": "https://linkedin.example/1", "description": "python llm"}


def _params(user_id=None, **extra) -> dict:
    p = {"user_id": user_id, "resume_summary": "AI engineer", "skills": ["python"],
         "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 0, "filters": {}, "include_seen": False}
    p.update(extra)
    p["profile_hash"] = search_profiles.profile_hash(p)
    return p


@pytest.fixture
def fake_pipeline(monkeypatch):
    """scrape_all returns per-keyword lists; the LLM call is recorded."""
    state = {"by_keyword": {}, "llm_calls": [], "during_llm": None}
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all",
                        lambda **kw: [dict(j) for j in state["by_keyword"].get(kw["keywords"][0], [])])

    def fake_llm(job, **kw):
        state["llm_calls"].append(job["url"])
        if state["during_llm"]:
            state["during_llm"]()
        return {**job, "llm_score": 90, "llm_reason": "fit"}

    monkeypatch.setattr(celery_tasks, "_score_one_job_llm", fake_llm)
    return state


def _run(state, keywords_to_jobs: dict, params: dict) -> str:
    state["by_keyword"] = keywords_to_jobs
    run_id = pipeline_progress.create_run(user_id=params.get("user_id") or "u", total_keywords=len(keywords_to_jobs))
    for kw in keywords_to_jobs:
        celery_tasks.search_and_score_keyword(run_id, kw, ["Pune"], ["linkedin", "indeed"], {}, params)
    return run_id


def _feed(run_id: str) -> list[dict]:
    """What a client sees after upserting entries by id."""
    by_id: dict[str, dict] = {}
    for entry in pipeline_progress.get_jobs(run_id=run_id):
        by_id[entry["id"]] = entry
    return list(by_id.values())


# ---------------------------------------------------------------- acceptance: unique jobs, LinkedIn kept

def test_indeed_first_then_linkedin_in_another_keyword_task_shows_linkedin_once(fake_pipeline):
    run_id = _run(fake_pipeline, {"AI Engineer": [INDEED], "LLM Developer": [LINKEDIN]}, _params())

    assert len(fake_pipeline["llm_calls"]) == 1  # scored once
    feed = _feed(run_id)
    assert len(feed) == 1
    assert feed[0]["source"] == "LinkedIn" and feed[0]["url"] == LINKEDIN["url"]
    assert feed[0]["llm_score"] == 90 and feed[0]["auto_apply_capable"] is True
    raw = pipeline_progress.get_jobs(run_id=run_id)
    assert [e.get("replaces", False) for e in raw] == [False, True]

    db = SessionLocal()
    try:
        job = db.get(Job, feed[0]["id"])
        assert job.best_source == "LinkedIn"
        assert {s.source for s in db.query(JobSourceSeen).filter_by(job_id=job.id)} == {"Indeed", "LinkedIn"}
    finally:
        db.close()


def test_linkedin_first_is_never_replaced_by_indeed(fake_pipeline):
    li = {**LINKEDIN, "company": "Zeta Labs", "url": "https://linkedin.example/2"}
    ind = {**INDEED, "company": "Zeta Labs Inc", "url": "https://indeed.example/2"}
    run_id = _run(fake_pipeline, {"a": [li], "b": [ind]}, _params())
    raw = pipeline_progress.get_jobs(run_id=run_id)
    assert len(raw) == 1 and raw[0]["source"] == "LinkedIn"


def test_linkedin_copy_stored_while_indeed_copy_is_being_scored_still_wins(fake_pipeline):
    ind = {**INDEED, "company": "Race Co", "url": "https://indeed.example/3"}
    li = {**LINKEDIN, "company": "Race Co", "url": "https://linkedin.example/3"}

    def linkedin_lands_mid_scoring():
        db = SessionLocal()
        try:
            job_store.upsert_copy(db, li, celery_tasks.job_key(li))
        finally:
            db.close()

    fake_pipeline["during_llm"] = linkedin_lands_mid_scoring
    run_id = _run(fake_pipeline, {"a": [ind]}, _params())
    feed = _feed(run_id)
    assert len(feed) == 1 and feed[0]["source"] == "LinkedIn"


def test_longer_description_wins_between_non_linkedin_copies():
    db = SessionLocal()
    try:
        short = {**INDEED, "company": "Desc Co", "source": "Naukri", "description": "short"}
        long_ = {**INDEED, "company": "Desc Co", "description": "much longer description here"}
        key = celery_tasks.job_key(short)
        job_store.upsert_copy(db, short, key)
        row = job_store.upsert_copy(db, long_, key)
        assert row.best_source == "Indeed"
        row = job_store.upsert_copy(db, short, key)
        assert row.best_source == "Indeed"
    finally:
        db.close()


# ---------------------------------------------------------------- acceptance: seen jobs not re-shown

def test_seen_job_is_not_shown_or_scored_again(fake_pipeline, make_user):
    user_id, _ = make_user()
    job = {**LINKEDIN, "company": "Seen Co", "url": "https://linkedin.example/seen"}
    first = _run(fake_pipeline, {"a": [job]}, _params(user_id))
    assert len(_feed(first)) == 1

    second = _run(fake_pipeline, {"a": [job]}, _params(user_id))
    assert _feed(second) == []
    assert len(fake_pipeline["llm_calls"]) == 1

    again = _run(fake_pipeline, {"a": [job]}, _params(user_id, include_seen=True))
    feed = _feed(again)
    assert len(feed) == 1 and feed[0]["llm_score"] == 90
    assert len(fake_pipeline["llm_calls"]) == 1  # stored score reused


def test_hidden_job_never_comes_back(fake_pipeline, make_user, client):
    user_id, headers = make_user()
    job = {**LINKEDIN, "company": "Hidden Co", "url": "https://linkedin.example/hidden"}
    job_id = _feed(_run(fake_pipeline, {"a": [job]}, _params(user_id)))[0]["id"]
    assert client.post(f"{API}/jobs/{job_id}/hide", headers=headers).json()["state"] == "hidden"
    assert _feed(_run(fake_pipeline, {"a": [job]}, _params(user_id, include_seen=True))) == []


def test_filtered_job_is_rescored_only_when_the_profile_changes(fake_pipeline, make_user):
    user_id, _ = make_user()
    job = {**LINKEDIN, "company": "Filter Co", "url": "https://linkedin.example/f", "description": "java"}
    strict = _params(user_id, keyword_prefilter_min_pct=50)
    assert _feed(_run(fake_pipeline, {"a": [job]}, strict)) == []
    assert _feed(_run(fake_pipeline, {"a": [job]}, strict)) == []
    db = SessionLocal()
    try:
        assert db.query(UserJob).filter_by(user_id=user_id).one().state == "filtered"
    finally:
        db.close()
    changed = _params(user_id, skills=["java"], keyword_prefilter_min_pct=50)
    assert len(_feed(_run(fake_pipeline, {"a": [job]}, changed))) == 1


def test_job_is_retried_when_every_ai_model_failed(fake_pipeline, make_user, monkeypatch):
    user_id, _ = make_user()
    job = {**LINKEDIN, "company": "Retry Co", "url": "https://linkedin.example/retry"}
    real_llm = celery_tasks._score_one_job_llm
    monkeypatch.setattr(celery_tasks, "_score_one_job_llm", lambda j, **kw: {
        **j, "llm_failed": True, "llm_score": 0, "llm_reason": "LLM scoring unavailable for this job."})
    assert _feed(_run(fake_pipeline, {"a": [job]}, _params(user_id, llm_min_score_to_keep=70))) == []
    db = SessionLocal()
    try:
        assert db.query(UserJob).filter_by(user_id=user_id).count() == 0  # not stored as filtered
    finally:
        db.close()
    monkeypatch.setattr(celery_tasks, "_score_one_job_llm", real_llm)
    assert len(_feed(_run(fake_pipeline, {"a": [job]}, _params(user_id, llm_min_score_to_keep=70)))) == 1


def test_seen_state_does_not_overwrite_saved(make_user):
    user_id, _ = make_user()
    db = SessionLocal()
    try:
        row = job_store.upsert_copy(db, {**LINKEDIN, "company": "Sticky"}, "sticky|ai engineer|pune")
        job_store.set_state(db, user_id=user_id, job_id=row.id, state="saved")
        db.commit()
        uj = job_store.record_user_job(db, user_id=user_id, job_id=row.id, state="seen", keyword_score=1,
                                       llm_score=2, reason="r", profile_hash="h", search_profile_id=None, run_id="x")
        assert uj.state == "saved" and uj.llm_score == 2
    finally:
        db.close()


# ---------------------------------------------------------------- filters and weighted skills

def test_must_have_exclude_and_avoided_companies_drop_jobs(fake_pipeline):
    base = {**LINKEDIN, "description": "python kubernetes remote"}
    jobs = [{**base, "company": "Keep Co", "url": "u1"},
            {**base, "company": "Bad Co Pvt Ltd", "url": "u2"},
            {**base, "company": "Excl Co", "url": "u3", "description": "python kubernetes onsite only"},
            {**base, "company": "NoK8s Co", "url": "u4", "description": "python remote"}]
    filters = {"avoid_companies": ["Bad Co"], "exclude": ["onsite only"], "must_have": ["kubernetes"]}
    run_id = _run(fake_pipeline, {"a": jobs}, _params(filters=filters))
    assert [j["company"] for j in _feed(run_id)] == ["Keep Co"]


def test_weighted_keyword_score_uses_synonyms_weights_and_symbol_terms():
    terms = search_profiles.skill_terms(
        [{"name": "kubernetes", "weight": "core", "synonyms": ["k8s"]},
         {"name": "c++", "weight": "familiar", "synonyms": []},
         {"name": "go", "weight": "strong", "synonyms": ["golang"]}],
        ["aws"],
    )
    text = "we run k8s and write c++ services"
    assert search_profiles.weighted_keyword_score(text, terms) == round(4 / 7 * 100, 1)
    assert search_profiles.contains("node.js and .net", ".net")
    assert not search_profiles.contains("going", "go")


# ---------------------------------------------------------------- profile + preferences

def test_profile_and_preferences_round_trip(client, make_user):
    _, headers = make_user()
    assert client.get(f"{API}/profile", headers=headers).json()["applicant_info"]["email"].endswith("@example.com")
    body = {"headline": "AI Engineer", "applicant_info": {"first_name": "A", "notice_period_days": 30},
            "work_authorization": [{"question": "Authorized in India?", "answer": "Yes"}]}
    assert client.put(f"{API}/profile", json=body, headers=headers).status_code == 200
    got = client.get(f"{API}/profile", headers=headers).json()
    assert got["headline"] == "AI Engineer" and got["applicant_info"]["notice_period_days"] == 30

    prefs = {"titles": ["AI Engineer", "ai engineer", " "], "locations": ["Pune"], "work_modes": ["remote"],
             "avoid_companies": ["Acme"], "posted_within_hours": 48, "salary_min": 10, "salary_max": 20}
    r = client.put(f"{API}/preferences", json=prefs, headers=headers)
    assert r.status_code == 200 and r.json()["titles"] == ["AI Engineer"]
    assert client.put(f"{API}/preferences", json={"salary_min": 5, "salary_max": 1}, headers=headers).status_code == 422
    assert client.put(f"{API}/preferences", json={"work_modes": ["moon"]}, headers=headers).status_code == 422


# ---------------------------------------------------------------- search profiles

PROFILE = {"name": "Backend", "titles": ["Backend Engineer"],
           "skills": [{"name": "Python", "weight": "core", "synonyms": ["py"]}],
           "must_have": ["python"], "exclude": ["senior staff"]}


def test_search_profile_crud_default_and_ownership(client, make_user):
    _, headers = make_user()
    _, other = make_user()
    a = client.post(f"{API}/search-profiles", json=PROFILE, headers=headers)
    assert a.status_code == 201 and a.json()["is_default"] is True
    assert client.post(f"{API}/search-profiles", json={**PROFILE, "name": "backend"},
                       headers=headers).json()["detail"]["code"] == "search_profile_name_taken"
    b = client.post(f"{API}/search-profiles", json={**PROFILE, "name": "AI", "is_default": True}, headers=headers)
    listed = client.get(f"{API}/search-profiles", headers=headers).json()["profiles"]
    assert [(p["name"], p["is_default"]) for p in listed] == [("AI", True), ("Backend", False)]

    pid = a.json()["id"]
    assert client.get(f"{API}/search-profiles/{pid}", headers=other).status_code == 404
    assert client.put(f"{API}/search-profiles/{pid}", json=PROFILE, headers=other).status_code == 404
    upd = client.put(f"{API}/search-profiles/{pid}", json={**PROFILE, "titles": ["Platform Engineer"]}, headers=headers)
    assert upd.json()["titles"] == ["Platform Engineer"]

    assert client.delete(f"{API}/search-profiles/{b.json()['id']}", headers=headers).status_code == 204
    assert client.get(f"{API}/search-profiles", headers=headers).json()["profiles"][0]["is_default"] is True
    dup_skill = {**PROFILE, "name": "X", "skills": [{"name": "Go"}, {"name": "go"}]}
    assert client.post(f"{API}/search-profiles", json=dup_skill, headers=headers).status_code == 422


def test_pipeline_with_search_profile_uses_profile_resume_and_preferences(client, make_user, dispatched):
    user_id, headers = make_user()
    pid = client.post(f"{API}/search-profiles", json=PROFILE, headers=headers).json()["id"]

    r = client.post(f"{API}/pipeline/run/stream", json={"search_profile_id": pid}, headers=headers)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "resume_required"

    _upload(client, headers, "Senior AI engineer with Python and FastAPI experience.")
    client.put(f"{API}/preferences", json={"locations": ["Bengaluru"], "posted_within_hours": 72,
                                          "avoid_companies": ["Acme Pvt Ltd"]}, headers=headers)
    dispatched.clear()
    r = client.post(f"{API}/pipeline/run/stream",
                    json={"search_profile_id": pid, "include_seen": True, "thresholds": {"llm_min_score_to_keep": 60}},
                    headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["keywords"] == ["Backend Engineer"]
    _, search, score = next(c for c in dispatched if c["task"] == "job_finder.dispatch_pipeline_group")["args"]
    assert search["keywords"] == ["Backend Engineer"] and search["locations"] == ["Bengaluru"]
    assert search["max_age_hours"] == 72
    assert score["skill_terms"] == [{"terms": ["Python", "py"], "weight": 3}]
    assert score["filters"] == {"avoid_companies": ["acme"], "must_have": ["python"], "exclude": ["senior staff"]}
    assert "Python and FastAPI" in score["resume_summary"]
    assert score["llm_min_score_to_keep"] == 60 and score["include_seen"] is True
    assert score["search_profile_id"] == pid and len(score["profile_hash"]) == 32


def test_pipeline_legacy_body_still_works_and_needs_skills(client, make_user, dispatched):
    _, headers = make_user()
    legacy = {"search": {"keywords": ["AI Engineer"], "sources": ["linkedin"]},
              "score": {"resume_summary": "AI engineer", "skills": ["python"]}}
    r = client.post(f"{API}/pipeline/run/stream", json=legacy, headers=headers)
    assert r.status_code == 202, r.text
    _, _, score = dispatched[-1]["args"]
    assert score["skills"] == ["python"] and "skill_terms" not in score and score["filters"] == {"avoid_companies": []}
    no_skills = {**legacy, "score": {"resume_summary": "x"}}
    assert client.post(f"{API}/pipeline/run/stream", json=no_skills, headers=headers).status_code == 422
    assert client.post(f"{API}/pipeline/run/stream", json={}, headers=headers).status_code == 422


def test_pipeline_rejects_someone_elses_profile(client, make_user):
    _, headers = make_user()
    _, other = make_user()
    pid = client.post(f"{API}/search-profiles", json=PROFILE, headers=headers).json()["id"]
    r = client.post(f"{API}/pipeline/run/stream", json={"search_profile_id": pid}, headers=other)
    assert r.status_code == 404


# ---------------------------------------------------------------- resumes

def _docx(text: str) -> bytes:
    from docx import Document
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _upload(client, headers, text: str, name: str = "cv.docx", **form):
    return client.post(f"{API}/resumes", headers=headers, data=form,
                       files={"file": (name, _docx(text), "application/octet-stream")})


def test_resume_upload_versions_master_and_encrypted_file(client, make_user, dispatched):
    user_id, headers = make_user()
    _, other = make_user()
    r = _upload(client, headers, "Jane Doe — Python engineer", label="Main")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["version"] == 1 and first["is_master"] and first["parse_status"] == "pending"
    assert "Python engineer" in first["text"] and first["file_name"] == "cv.docx"
    assert any(c["task"] == "job_finder.parse_resume" and c["args"] == [first["id"], user_id] for c in dispatched)

    assert _upload(client, headers, "Jane Doe — Python engineer").json()["detail"]["code"] == "resume_duplicate"
    second = _upload(client, headers, "Jane Doe v2", parse="false").json()
    assert second["version"] == 2 and not second["is_master"] and second["parse_status"] == "unparsed"

    db = SessionLocal()
    try:
        from app.models.profile import ResumeFile
        assert b"Jane" not in db.get(ResumeFile, first["id"]).ciphertext.encode()
    finally:
        db.close()
    f = client.get(f"{API}/resumes/{first['id']}/file", headers=headers)
    assert f.status_code == 200 and f.content == _docx_bytes_roundtrip(f.content)
    assert client.get(f"{API}/resumes/{first['id']}", headers=other).status_code == 404
    assert client.get(f"{API}/resumes/{first['id']}/file", headers=other).status_code == 404

    assert client.post(f"{API}/resumes/{second['id']}/master", headers=headers).json()["is_master"] is True
    listing = client.get(f"{API}/resumes", headers=headers).json()
    assert listing["master_id"] == second["id"] and [x["version"] for x in listing["resumes"]] == [2, 1]
    assert "text" not in listing["resumes"][0]

    assert client.delete(f"{API}/resumes/{second['id']}", headers=headers).status_code == 204
    assert client.get(f"{API}/resumes", headers=headers).json()["master_id"] == first["id"]


def _docx_bytes_roundtrip(content: bytes) -> bytes:
    assert resume_store.sniff_type(content) == resume_store.DOCX
    return content


def test_resume_upload_rejects_bad_files(client, make_user, monkeypatch):
    _, headers = make_user()
    bad = client.post(f"{API}/resumes", headers=headers, files={"file": ("cv.pdf", b"hello", "application/pdf")})
    assert bad.status_code == 415
    broken_pdf = client.post(f"{API}/resumes", headers=headers,
                             files={"file": ("cv.pdf", b"%PDF-1.4 garbage", "application/pdf")})
    assert broken_pdf.status_code == 422 and broken_pdf.json()["detail"]["code"] == "resume_unreadable"
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "RESUME_MAX_BYTES", 100)
    assert _upload(client, headers, "x" * 10).status_code == 413


class _FakeResult:
    engine = "groq:test-model"


def test_resume_parse_and_review(client, make_user, monkeypatch):
    user_id, headers = make_user()
    rid = _upload(client, headers, "Jane Doe. Skills: Python, FastAPI").json()["id"]
    reply = ('```json\n{"name": "Jane Doe", "skills": ["Python", "FastAPI"], '
             '"experience": [{"title": "AI Engineer", "company": "Acme", "current": true}]}\n```')

    def fake_complete(db, **kw):
        assert kw["task"] == "resume_parse" and "Jane Doe" in kw["user_prompt"]
        assert kw["validate"]("not json") is False
        assert kw["validate"](reply) is True
        return _FakeResult()

    monkeypatch.setattr(resume_store.ai_router, "complete", fake_complete)
    assert celery_tasks.parse_resume_task(rid, user_id)["status"] == "parsed"
    got = client.get(f"{API}/resumes/{rid}", headers=headers).json()
    assert got["parsed"]["skills"] == ["Python", "FastAPI"] and got["parsed_by"] == "groq:test-model"

    parsed = {**got["parsed"], "skills": ["Python", "FastAPI", "Docker"]}
    put = client.put(f"{API}/resumes/{rid}", json={"parsed": parsed}, headers=headers).json()
    assert put["parse_status"] == "reviewed" and put["parsed"]["skills"][-1] == "Docker"

    seeded = client.post(f"{API}/search-profiles/from-resume", json={}, headers=headers)
    assert seeded.status_code == 201
    assert [s["name"] for s in seeded.json()["skills"]] == ["Python", "FastAPI", "Docker"]
    assert seeded.json()["titles"] == ["AI Engineer"]
    again = client.post(f"{API}/search-profiles/from-resume", json={}, headers=headers).json()
    assert again["name"] == "From my resume (2)"


def test_resume_parse_failure_is_recorded(client, make_user, monkeypatch):
    user_id, headers = make_user()
    rid = _upload(client, headers, "Some resume").json()["id"]

    def no_model(db, **kw):
        raise resume_store.ai_router.NoModelAvailable("resume_parse", ["Groq: quota"])

    monkeypatch.setattr(resume_store.ai_router, "complete", no_model)
    assert celery_tasks.parse_resume_task(rid, user_id)["status"] == "failed"
    assert "quota" in client.get(f"{API}/resumes/{rid}", headers=headers).json()["parse_error"]
    assert celery_tasks.parse_resume_task(rid, "someone-else")["status"] == "missing"


def test_suggest_titles(client, make_user, monkeypatch):
    _, headers = make_user()
    pid = client.post(f"{API}/search-profiles", json=PROFILE, headers=headers).json()["id"]

    def fake_complete(db, **kw):
        assert kw["task"] == "title_suggest"
        assert kw["validate"]('{"titles": ["Backend Engineer", "Python Developer", "API Engineer"]}')
        return _FakeResult()

    monkeypatch.setattr(search_profiles.ai_router, "complete", fake_complete)
    r = client.post(f"{API}/search-profiles/{pid}/suggest-titles", headers=headers)
    assert r.json()["titles"] == ["Python Developer", "API Engineer"]


# ---------------------------------------------------------------- saved / hidden jobs

def test_save_hide_and_list_jobs(client, make_user):
    _, headers = make_user()
    _, other = make_user()
    db = SessionLocal()
    try:
        job_id = job_store.upsert_copy(db, {**LINKEDIN, "company": "Save Co"}, "save co|ai engineer|pune").id
    finally:
        db.close()
    assert client.post(f"{API}/jobs/{job_id}/save", headers=headers).json()["state"] == "saved"
    saved = client.get(f"{API}/jobs", headers=headers).json()
    assert saved["total"] == 1 and saved["jobs"][0]["sources"] == ["LinkedIn"]
    assert client.get(f"{API}/jobs", headers=other).json()["total"] == 0
    client.post(f"{API}/jobs/{job_id}/hide", headers=headers)
    assert client.get(f"{API}/jobs?state=hidden", headers=headers).json()["total"] == 1
    assert client.post(f"{API}/jobs/nope/save", headers=headers).status_code == 404
    assert client.post(f"{API}/jobs/{job_id}/delete", headers=headers).status_code == 422
