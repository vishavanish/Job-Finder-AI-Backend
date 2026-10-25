"""QA2 §3: Applicant info is filled from the master resume (empty fields only),
years of experience come from the job dates when the parser leaves them out,
and profile links lose their tracking parameters."""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.core import celery_tasks
from app.models.profile_schemas import ParsedResume
from app.services import profile_fill, resume_store

API = "/api/v1"
PARSED = {
    "name": "AVANISH VISHWAKARMA", "email": "avanish@example.com", "phone": "8356013043",
    "location": "Thane, Mumbai", "skills": ["Python"],
    "experience": [
        {"title": "Senior Software Developer", "company": "Osource", "start": "09/2025", "end": "Present"},
        {"title": "Jr. Software Engineer", "company": "Datamatics", "start": "05/2024", "end": "09/2025"},
        {"title": "Trainer", "company": "Squad", "start": "12/2023", "end": "02/2024"},
    ],
    "links": [{"label": "LinkedIn", "url": "linkedin.com/in/avanish/?isSelfProfile=true&utm_source=x"},
              {"label": "GitHub", "url": "https://github.com/vishavanish"}],
}


@pytest.mark.parametrize("url, clean", [
    ("https://www.linkedin.com/in/avanishvishwakarmapy/?isSelfProfile=true",
     "https://www.linkedin.com/in/avanishvishwakarmapy/"),
    ("linkedin.com/in/x?utm_source=share&trk=public", "https://linkedin.com/in/x"),
    ("https://site.dev/work?page=2#top", "https://site.dev/work?page=2"),
    ("", ""),
])
def test_clean_url(url, clean):
    assert profile_fill.clean_url(url) == clean


def test_experience_years_from_job_dates_counts_overlaps_once():
    parsed = ParsedResume.model_validate(PARSED)
    # Dec 2023 – Feb 2024 (3 months) + May 2024 – Oct 2026 (30, the Sep 2025 overlap once) = 33 months = 2.75 y
    assert profile_fill.experience_years(parsed, today=date(2026, 10, 25)) == 3.0  # half-year steps
    assert profile_fill.experience_years(ParsedResume.model_validate({**PARSED, "total_experience_years": 3}),
                                         today=date(2026, 10, 25)) == 3
    assert profile_fill.experience_years(ParsedResume.model_validate({"experience": []})) is None


def test_from_resume_maps_name_contact_and_links():
    got = profile_fill.from_resume(ParsedResume.model_validate(PARSED))
    assert got["first_name"] == "Avanish" and got["last_name"] == "Vishwakarma"
    assert got["phone"] == "8356013043" and got["city"] == "Thane" and got["country"] == "Mumbai"
    assert got["linkedin_url"] == "https://linkedin.com/in/avanish/"
    assert got["github_url"] == "https://github.com/vishavanish"


class _FakeResult:
    engine = "groq:test-model"


def _upload(client, headers, text):
    from tests.test_profile_search import _upload as upload
    return upload(client, headers, text)


def test_parse_fills_only_empty_applicant_fields(client, make_user, monkeypatch):
    user_id, headers = make_user()
    profile = client.get(f"{API}/profile", headers=headers).json()
    profile["applicant_info"].update(first_name="Avi", phone="")  # typed by the user: kept
    assert client.put(f"{API}/profile", json=profile, headers=headers).status_code == 200

    rid = _upload(client, headers, "Avanish resume text").json()["id"]
    monkeypatch.setattr(resume_store.ai_router, "complete",
                        lambda db, **kw: (kw["validate"](json.dumps(PARSED)), _FakeResult())[1])
    assert celery_tasks.parse_resume_task(rid, user_id)["status"] == "parsed"

    info = client.get(f"{API}/profile", headers=headers).json()["applicant_info"]
    assert info["first_name"] == "Avi"  # never overwritten
    assert info["last_name"] == "Vishwakarma" and info["phone"] == "8356013043"
    assert info["github_url"] == "https://github.com/vishavanish"
    assert info["total_experience_years"] is not None
    parsed = client.get(f"{API}/resumes/{rid}", headers=headers).json()["parsed"]
    assert parsed["total_experience_years"] == info["total_experience_years"]


def test_saving_the_profile_drops_tracking_parameters(client, make_user):
    _, headers = make_user()
    profile = client.get(f"{API}/profile", headers=headers).json()
    profile["applicant_info"]["linkedin_url"] = "https://www.linkedin.com/in/me/?isSelfProfile=true"
    saved = client.put(f"{API}/profile", json=profile, headers=headers).json()
    assert saved["applicant_info"]["linkedin_url"] == "https://www.linkedin.com/in/me/"
