"""QA10 — resume check (can a resume reader read the file), keyword fit
per job, plain copy. Test files are built in code (tests/resume_fixtures.py)."""
from __future__ import annotations

import pytest

from app.core import celery_tasks
from app.core.db import SessionLocal
from app.models.jobs import Job
from app.models.profile import Resume
from app.models.profile_schemas import ParsedResume
from app.services import apply_prep, job_store, resume_check, resume_store
from tests.resume_fixtures import LOREM, make_docx, make_pdf, one_column_lines, two_column_lines

API = "/api/v1"
PDF, DOCX = resume_check.PDF, resume_check.DOCX


def _levels(result: dict) -> dict[str, str]:
    """Worst level per finding id."""
    order = {"pass": 0, "info": 1, "warn": 2, "fail": 3}
    out: dict[str, str] = {}
    for f in result["findings"]:
        if order[f["level"]] >= order.get(out.get(f["id"], "pass"), 0):
            out[f["id"]] = f["level"]
    return out


# ---------------------------------------------------------------- the check itself

def test_clean_one_column_pdf_is_good_and_reads_in_order():
    r = resume_check.check(PDF, make_pdf([one_column_lines()]))
    assert r["band"] == "good" and r["score"] == 100, r["findings"]
    lv = _levels(r)
    assert lv["text_layer"] == lv["columns"] == lv["contact"] == lv["headings"] == "pass"
    assert r["pages"] == 1 and r["words"] > 300
    assert r["seen_text"].index("Experience") < r["seen_text"].index("Education") < r["seen_text"].index("Skills")


def test_two_column_pdf_needs_fixes():
    r = resume_check.check(PDF, make_pdf([two_column_lines()]))
    assert _levels(r)["columns"] == "warn" and r["band"] == "needs_fixes"


def test_right_aligned_dates_are_not_columns():
    page = {"width": 612, "height": 792, "images": 0,
            "lines": [{"text": LOREM, "x0": 50, "x1": 560, "y0": 700 - i * 14, "y1": 710 - i * 14} for i in range(20)]
            + [{"text": "Jan 2022 - Present", "x0": 480, "x1": 560, "y0": 300 - i * 30, "y1": 310 - i * 30}
               for i in range(5)]}
    assert resume_check.has_columns(page) is False


def test_image_only_pdf_is_hard_to_read():
    r = resume_check.check(PDF, make_pdf([[]], image_only=True))
    assert r["score"] == 0 and r["band"] == "hard_to_read" and _levels(r)["text_layer"] == "fail"


def test_contact_only_in_repeated_page_header_fails():
    header = [(50, 770, "Asha Rao | asha.rao@example.com | +91 98765 43210")]
    body = [line for line in one_column_lines(top=720) if "@" not in line[2]]
    r = resume_check.check(PDF, make_pdf([header + body, header + body]))
    assert _levels(r)["contact"] == "fail"


@pytest.mark.parametrize("variant, finding, level", [
    ({"contact_in_header": True}, "contact", "fail"),
    ({"text_box": True}, "text_boxes", "fail"),
    ({"layout_table": True}, "columns", "warn"),
    ({"two_columns": True}, "columns", "warn"),
    ({"sections": False}, "headings", "warn"),
])
def test_docx_problems(variant, finding, level):
    clean = resume_check.check(DOCX, make_docx())
    assert all(f["level"] in ("pass", "info") for f in clean["findings"] if f["id"] != "length")
    r = resume_check.check(DOCX, make_docx(**variant))
    assert _levels(r)[finding] == level
    assert r["score"] < clean["score"]
    if variant.get("text_box"):
        assert LOREM.split()[0] in r["seen_text"]  # the body still reads; the box's text is what's missing


def test_odd_characters_from_icon_fonts():
    import io

    from docx import Document

    seen = "Asha Rao\n\ue001 asha@example.com \ue002 +91 98765 43210\n" + "(cid:12)" * 30 + "\n" + LOREM
    assert len(resume_check.ODD_RE.findall(seen)) == 32
    doc = Document()
    for line in seen.splitlines():
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    assert _levels(resume_check.check(DOCX, buf.getvalue()))["odd_characters"] == "fail"


def test_dates_and_length_from_the_parse_and_refresh_after_review():
    parsed = {"total_experience_years": 3, "experience": [
        {"title": "Engineer", "company": "Orbit", "start": "Jan 2022", "end": "", "current": True},
        {"title": "Intern", "company": "Acme", "start": "summer", "end": "later", "current": False}]}
    r = resume_check.check(PDF, make_pdf([one_column_lines()] * 3), parsed=parsed)
    lv = _levels(r)
    assert lv["dates"] == "warn" and lv["length"] == "warn"  # 3 pages with 3 years
    assert "Intern — Acme" in next(f for f in r["findings"] if f["id"] == "dates")["detail"]
    parsed["experience"][1].update(start="06/2021", end="08/2021")
    fixed = resume_check.refresh_parsed(r, parsed)
    assert _levels(fixed)["dates"] == "pass" and fixed["score"] > r["score"]


@pytest.mark.parametrize("value, ok", [("Jan 2022", True), ("January, 2022", True), ("03/2021", True), ("2019", True),
                                      ("Present", True), ("Sept '21", True), ("summer", False), ("", False)])
def test_readable_date(value, ok):
    assert resume_check.readable_date(value) is ok


def test_our_own_plain_docx_is_good():
    parsed = ParsedResume(name="Asha Rao", email="asha@example.com", phone="+91 98765 43210", summary=LOREM,
                          skills=["Python", "FastAPI"],
                          experience=[{"title": "Backend Engineer", "company": "Orbit", "start": "Jan 2022",
                                       "current": True, "highlights": [LOREM] * 8}],
                          education=[{"school": "Pune University", "degree": "B.Tech", "end": "2021"}])
    data, _ = apply_prep.render_docx(parsed)
    r = resume_check.check(DOCX, data, parsed=parsed.model_dump())
    assert r["band"] == "good" and r["score"] == 100, r["findings"]


# ---------------------------------------------------------------- keyword fit

def test_keyword_fit_aliases_boundaries_and_missing():
    fit = resume_check.keyword_fit(
        job_title="Python Backend Developer",
        job_text="Python, FastAPI, Postgres, Kubernetes and Kafka. JavaScript is a plus. C++ nice. "
                 "Strong communication. Bachelor's degree in CS.",
        resume_text="Backend developer. Python, FastAPI, PostgreSQL, Java, cpp. B.Tech.",
        resume_titles=["Python Backend Developer"], missing_required=["Terraform", "kafka"])
    assert {"python", "fastapi", "postgresql", "c++"} <= set(fit["hard_found"])
    assert "java" not in fit["hard_found"] + fit["hard_missing"]  # the job says JavaScript, not Java
    assert {"kubernetes", "kafka", "javascript", "terraform"} <= set(fit["hard_missing"])
    assert fit["hard_missing"].count("kafka") == 1
    assert fit["soft_missing"] == ["communication"]
    assert fit["title_found"] is True and fit["education"] == {"required": "Bachelor's", "met": True}
    assert fit["target"] == 75 and 0 < fit["fit"] < 100


def test_keyword_fit_with_nothing_to_compare():
    fit = resume_check.keyword_fit(job_title="", job_text="Join our friendly team.", resume_text="anything")
    assert fit["fit"] is None and fit["education"] == {"required": None, "met": None}


# ---------------------------------------------------------------- API

def _upload(client, headers, data: bytes, name="cv.pdf", ctype="application/pdf", parse="false"):
    return client.post(f"{API}/resumes", headers=headers, data={"parse": parse},
                       files={"file": (name, data, ctype)})


def _run_checks(dispatched):
    for c in [c for c in dispatched if c["task"] == "job_finder.check_resume"]:
        celery_tasks.check_resume_task(*c["args"])
    dispatched[:] = [c for c in dispatched if c["task"] != "job_finder.check_resume"]


def test_upload_queues_the_check_and_detail_shows_it(client, make_user, dispatched):
    _, headers = make_user()
    r = _upload(client, headers, make_pdf([two_column_lines()]))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["ats_check"] is None
    assert [c["args"][0] for c in dispatched if c["task"] == "job_finder.check_resume"] == [rid]
    _run_checks(dispatched)
    body = client.get(f"{API}/resumes/{rid}", headers=headers).json()
    assert body["ats_check"]["band"] == "needs_fixes" and body["ats_checked_at"]
    assert any(f["id"] == "columns" and f["level"] == "warn" for f in body["ats_check"]["findings"])
    assert "Experience" in body["ats_check"]["seen_text"]
    listed = client.get(f"{API}/resumes", headers=headers).json()["resumes"][0]
    assert listed["ats_check"]["score"] == body["ats_check"]["score"]
    assert not [c for c in dispatched if c["task"] == "job_finder.check_resume"]  # current: nothing queued


def test_parse_task_runs_the_check_after_parsing(client, make_user, dispatched, monkeypatch):
    user_id, headers = make_user()
    rid = _upload(client, headers, make_pdf([one_column_lines()]), parse="true").json()["id"]
    assert not [c for c in dispatched if c["task"] == "job_finder.check_resume"]

    def fake_parse(db, row):
        row.parsed = {"experience": [{"title": "Engineer", "company": "Orbit", "start": "someday", "end": "",
                                      "current": True}]}
        row.parse_status = "parsed"
        return row
    monkeypatch.setattr(resume_store, "parse_resume", fake_parse)
    celery_tasks.parse_resume_task(rid, user_id)
    check = client.get(f"{API}/resumes/{rid}", headers=headers).json()["ats_check"]
    assert any(f["id"] == "dates" and f["level"] == "warn" for f in check["findings"])


def test_old_resumes_are_checked_lazily_once(client, make_user, dispatched):
    _, headers = make_user()
    rid = _upload(client, headers, make_docx(), name="cv.docx", ctype=DOCX).json()["id"]
    dispatched.clear()
    from app.core.redis_client import get_redis
    get_redis().flushall()
    client.get(f"{API}/resumes", headers=headers)
    client.get(f"{API}/resumes/{rid}", headers=headers)
    assert [c["args"][0] for c in dispatched if c["task"] == "job_finder.check_resume"] == [rid]


def test_old_check_version_counts_as_missing(client, make_user, dispatched):
    _, headers = make_user()
    rid = _upload(client, headers, make_docx(), name="cv.docx", ctype=DOCX).json()["id"]
    _run_checks(dispatched)
    db = SessionLocal()
    try:
        row = db.get(Resume, rid)
        row.ats_check = {**row.ats_check, "version": 0}
        db.commit()
    finally:
        db.close()
    from app.core.redis_client import get_redis
    get_redis().flushall()
    assert client.get(f"{API}/resumes/{rid}", headers=headers).json()["ats_check"] is None
    assert any(c["task"] == "job_finder.check_resume" for c in dispatched)


def test_recheck_and_other_users(client, make_user, dispatched):
    _, headers = make_user()
    _, other = make_user()
    rid = _upload(client, headers, make_docx(), name="cv.docx", ctype=DOCX).json()["id"]
    _run_checks(dispatched)
    assert client.post(f"{API}/resumes/{rid}/check", headers=other).status_code == 404
    r = client.post(f"{API}/resumes/{rid}/check", headers=headers)
    assert r.status_code == 202 and r.json()["ats_check"] is None
    assert any(c["task"] == "job_finder.check_resume" for c in dispatched)
    _run_checks(dispatched)
    assert client.get(f"{API}/resumes/{rid}", headers=headers).json()["ats_check"]["band"]


def test_missing_file_gives_an_error_result_not_a_crash(client, make_user, dispatched):
    _, headers = make_user()
    rid = _upload(client, headers, make_docx(), name="cv.docx", ctype=DOCX).json()["id"]
    db = SessionLocal()
    try:
        from app.models.profile import ResumeFile
        db.query(ResumeFile).filter(ResumeFile.resume_id == rid).delete()
        db.commit()
    finally:
        db.close()
    _run_checks(dispatched)
    check = client.get(f"{API}/resumes/{rid}", headers=headers).json()["ats_check"]
    assert check["score"] is None and "not stored" in check["error"]


def test_plain_copy(client, make_user, dispatched):
    _, headers = make_user()
    master = _upload(client, headers, make_pdf([two_column_lines()])).json()
    r = client.post(f"{API}/resumes/{master['id']}/plain-copy", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "resume_not_reviewed"

    parsed = {"name": "Asha Rao", "email": "asha@example.com", "phone": "+91 98765 43210", "summary": LOREM,
              "skills": ["Python"], "experience": [{"title": "Engineer", "company": "Orbit", "start": "Jan 2022",
                                                   "current": True, "highlights": [LOREM] * 8}],
              "education": [{"school": "Pune University", "degree": "B.Tech", "end": "2021"}]}
    assert client.put(f"{API}/resumes/{master['id']}", json={"parsed": parsed}, headers=headers).status_code == 200
    copy = client.post(f"{API}/resumes/{master['id']}/plain-copy", headers=headers)
    assert copy.status_code == 201, copy.text
    c = copy.json()
    assert c["parent_id"] == master["id"] and not c["is_master"] and c["label"].startswith("Plain copy of")
    assert c["file_name"].endswith(".docx") and c["ats_check"]["band"] == "good"
    file = client.get(f"{API}/resumes/{c['id']}/file", headers=headers)
    assert file.status_code == 200 and file.content[:2] == b"PK"
    # made master: it no longer hangs off the original, so deleting the original keeps it
    client.post(f"{API}/resumes/{c['id']}/master", headers=headers)
    assert client.delete(f"{API}/resumes/{master['id']}", headers=headers).status_code == 204
    assert client.get(f"{API}/resumes/{c['id']}", headers=headers).status_code == 200


def _job(**kw) -> str:
    db = SessionLocal()
    try:
        job = Job(job_key=f"k-{kw.get('title')}-{id(kw)}", title=kw.get("title", "Python Developer"), company="Orbit",
                  description=kw.get("description", "Python, FastAPI and Kubernetes. Bachelor's degree."))
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_keyword_fit_endpoint(client, make_user, dispatched):
    user_id, headers = make_user()
    job_id = _job()
    r = client.get(f"{API}/jobs/{job_id}/keyword-fit", headers=headers)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "resume_required"
    master = _upload(client, headers, make_pdf([one_column_lines()])).json()
    db = SessionLocal()
    try:
        job_store.set_state(db, user_id=user_id, job_id=job_id, state="seen")
        uj = job_store.get_user_job(db, user_id, job_id)
        uj.breakdown = {"missing_required": ["Terraform"]}
        db.commit()
    finally:
        db.close()
    body = client.get(f"{API}/jobs/{job_id}/keyword-fit", headers=headers).json()
    assert body["resume_id"] == master["id"]
    assert set(body["hard_found"]) == {"python", "fastapi"} and set(body["hard_missing"]) == {"kubernetes", "terraform"}
    assert body["education"] == {"required": "Bachelor's", "met": True}
    again = client.get(f"{API}/jobs/{job_id}/keyword-fit?resume_id={master['id']}", headers=headers).json()
    assert again == body
    # the match breakdown sent to the feed is untouched
    assert client.get(f"{API}/jobs?state=seen", headers=headers).json()["jobs"][0]["match"] == {
        "missing_required": ["Terraform"]}
    _, other = make_user()
    assert client.get(f"{API}/jobs/{job_id}/keyword-fit?resume_id={master['id']}", headers=other).status_code == 404
    assert client.get(f"{API}/jobs/nope/keyword-fit", headers=headers).status_code == 404
