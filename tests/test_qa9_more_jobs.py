"""QA9: more jobs per search. Title or skills pass the prefilter, 50-69 show as possible matches, Indeed asks for
100, a 7-day default, the career-page index filters by place before its limit, and jobs are counted per source."""
from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from docx import Document

from app.core import celery_tasks
from app.core.db import SessionLocal
from app.models.ats import AtsCompany
from app.models.jobs import UserJob
from app.providers.ats.platforms import AtsRef
from app.services import ats_crawler, ats_registry, ats_search, job_sources, pipeline_progress

API = "/api/v1"


def _params(user_id=None, **extra) -> dict:
    p = {"user_id": user_id, "resume_summary": "AI engineer", "skills": ["kubernetes"], "career_targets": ["AI Engineer"],
         "skill_terms": [{"terms": ["kubernetes"], "weight": 3}], "keyword_prefilter_min_pct": 30,
         "llm_min_score_to_keep": 70, "filters": {}, "include_seen": False}
    p.update(extra)
    p["profile_hash"] = "same-profile"
    return p


def _job(title: str, description: str = "a short snippet") -> dict:
    ref = uuid.uuid4().hex[:8]
    return {"source": "Adzuna", "title": title, "company": f"Co {ref}", "location": "Pune",
            "url": f"https://adzuna.example/{ref}", "description": description}


def _process(db, monkeypatch, jobs: list[dict], params: dict, score: float = 90,
             calls: list | None = None) -> tuple[str, dict]:
    def scorer(batch, **kw):
        if calls is not None:
            calls.extend(j["title"] for j in batch)
        return [{**j, "llm_score": score, "llm_reason": "r"} for j in batch]

    monkeypatch.setattr(celery_tasks, "_score_batch_llm", scorer)
    run_id = pipeline_progress.create_run(user_id="u-qa9", total_keywords=1)
    counts, _ = celery_tasks._process_jobs(db, run_id, jobs, score_params=params, request_keys={}, show_early=False)
    return run_id, counts


def _feed(run_id: str) -> list[dict]:
    return pipeline_progress.current_jobs(run_id=run_id)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def user_id(make_user):
    return make_user()[0]


# ---------------------------------------------------------------- A: prefilter

def test_a_fitting_title_reaches_the_ai_even_with_a_short_snippet(db, monkeypatch):
    calls: list[str] = []
    _, counts = _process(db, monkeypatch, [_job("Senior AI Engineer"), _job("Mechanical Engineer"), _job("Accountant")],
                         _params(), calls=calls)
    assert calls == ["Senior AI Engineer"]  # "Engineer" alone is not a fit
    assert counts["prefiltered"] == 2 and counts["kept"] == 1


def test_skills_alone_still_pass_the_prefilter(db, monkeypatch):
    _, counts = _process(db, monkeypatch, [_job("Platform Specialist", "kubernetes every day")], _params())
    assert counts["kept"] == 1 and counts["prefiltered"] == 0


def test_a_job_the_old_prefilter_stopped_gets_a_new_chance(db, monkeypatch, user_id):
    job = _job("AI Engineer")
    _process(db, monkeypatch, [job], _params(user_id, career_targets=[]))  # nothing fits: stopped, no score
    row = db.query(UserJob).filter(UserJob.user_id == user_id).one()
    assert row.state == "filtered" and row.llm_score is None
    calls: list[str] = []
    _, counts = _process(db, monkeypatch, [job], _params(user_id), calls=calls)
    assert calls == ["AI Engineer"] and counts["kept"] == 1


# ---------------------------------------------------------------- A: two groups

@pytest.mark.parametrize("score,group", [(85, "strong"), (70, "strong"), (60, "possible"), (50, "possible")])
def test_jobs_from_the_floor_up_show_in_a_group(db, monkeypatch, score, group):
    run_id, counts = _process(db, monkeypatch, [_job("AI Engineer")], _params(possible_min_score=50), score=score)
    assert counts["kept"] == 1 and _feed(run_id)[0]["match_group"] == group


def test_below_the_floor_is_not_shown(db, monkeypatch):
    run_id, counts = _process(db, monkeypatch, [_job("AI Engineer")], _params(possible_min_score=50), score=49)
    assert counts["filtered"] == 1 and _feed(run_id) == []


def test_a_slider_below_the_floor_still_wins(db, monkeypatch):
    run_id, counts = _process(db, monkeypatch, [_job("AI Engineer")],
                              _params(possible_min_score=50, llm_min_score_to_keep=30), score=40)
    assert counts["kept"] == 1 and _feed(run_id)[0]["match_group"] == "strong"


def test_job_alerts_have_no_possible_group(db, monkeypatch):
    run_id, counts = _process(db, monkeypatch, [_job("AI Engineer")], _params(), score=60)
    assert counts["filtered"] == 1 and _feed(run_id) == []


def test_earlier_low_scorers_come_back_as_possible_without_an_ai_call(db, monkeypatch, user_id):
    job = _job("AI Engineer")
    _process(db, monkeypatch, [job], _params(user_id), score=60)
    calls: list[str] = []
    run_id, counts = _process(db, monkeypatch, [job], _params(user_id, possible_min_score=50), calls=calls)
    assert calls == [] and counts["kept"] == 1
    assert _feed(run_id)[0]["match_group"] == "possible" and _feed(run_id)[0]["llm_score"] == 60


# ---------------------------------------------------------------- B: Indeed asks for more, LinkedIn does not

@pytest.fixture
def jobspy_calls(monkeypatch):
    calls: list[tuple[list[str], int]] = []

    def fake(keyword, location, sites, results_per_keyword, **kw):
        calls.append((sites, results_per_keyword))
        return None, None

    monkeypatch.setattr(job_sources, "_run_jobspy", fake)
    monkeypatch.setattr(job_sources, "_normalize", lambda df: [])
    return calls


def test_indeed_gets_its_own_call_and_linkedin_stays_at_25(jobspy_calls):
    job_sources.scrape_all(["AI"], ["Pune"], ["linkedin", "indeed"], indeed_results_per_keyword=100)
    assert jobspy_calls == [(["indeed"], 100), (["linkedin"], 25)]


def test_without_the_setting_there_is_one_call_as_before(jobspy_calls):
    job_sources.scrape_all(["AI"], ["Pune"], ["linkedin", "indeed"])
    assert jobspy_calls == [(["linkedin", "indeed"], 25)]


def test_indeed_alone_makes_one_call(jobspy_calls):
    job_sources.scrape_all(["AI"], ["Pune"], ["indeed"], indeed_results_per_keyword=100)
    assert jobspy_calls == [(["indeed"], 100)]


# ---------------------------------------------------------------- A, B, C, D: what a Jobs search asks for

PROFILE = {"name": "AI", "titles": ["AI Engineer"], "skills": [{"name": "Python", "weight": "core"}]}


def _dispatched_params(client, headers, dispatched, pid):
    buffer = BytesIO()
    doc = Document()
    doc.add_paragraph("AI engineer with Python experience.")
    doc.save(buffer)
    media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    client.post(f"{API}/resumes", files={"file": ("r.docx", buffer.getvalue(), media)}, headers=headers)
    dispatched.clear()
    assert client.post(f"{API}/pipeline/run/stream", json={"search_profile_id": pid},
                       headers=headers).status_code == 202
    _, search, score = next(c for c in dispatched if c["task"] == "job_finder.dispatch_pipeline_group")["args"]
    return search, score


def test_a_jobs_search_defaults_to_seven_days_and_asks_for_more(client, make_user, dispatched):
    _, headers = make_user()
    pid = client.post(f"{API}/search-profiles", json=PROFILE, headers=headers).json()["id"]
    search, score = _dispatched_params(client, headers, dispatched, pid)
    assert search["max_age_hours"] == 168
    assert search["indeed_results_per_keyword"] == 100 and search["index_results_per_keyword"] == 100
    assert score["possible_min_score"] == 50 and score["llm_min_score_to_keep"] == 70


def test_profile_posted_within_still_wins(client, make_user, dispatched):
    _, headers = make_user()
    pid = client.post(f"{API}/search-profiles", json=PROFILE, headers=headers).json()["id"]
    client.put(f"{API}/preferences", json={"posted_within_hours": 24}, headers=headers)
    search, _ = _dispatched_params(client, headers, dispatched, pid)
    assert search["max_age_hours"] == 24


# ---------------------------------------------------------------- D: place before the limit

def _company(db, slug: str, location: str, title: str, n: int = 1) -> AtsCompany:
    row, _ = ats_registry.get_or_create(db, AtsRef("ashby", slug), origin="admin", name=slug.title())
    ats_crawler.upsert_jobs(db, row, [
        {"external_id": f"{slug}-{i}", "title": title, "location": location, "department": "",
         "url": f"https://jobs.example/{slug}/{i}", "posted": "", "remote": False, "country": "",
         "description": "Build things"} for i in range(n)], complete=False, close=False)
    db.commit()
    return row


def test_jobs_elsewhere_in_the_world_do_not_use_up_the_limit(db):
    slug = f"qa9{uuid.uuid4().hex[:6]}"
    _company(db, f"{slug}a", "Berlin, Germany", "Zebra Handler", n=40)
    _company(db, f"{slug}b", "Pune, India", "Zebra Handler")
    jobs = ats_search.index_jobs(db, "zebra handler", locations=["Pune"], country="India", max_age_hours=24,
                                 limit=1, detail_budget=0)
    assert [j["location"] for j in jobs] == ["Pune, India"]


def test_a_city_alias_and_a_whole_country_still_match(db):
    slug = f"qa9{uuid.uuid4().hex[:6]}"
    _company(db, f"{slug}a", "Bengaluru, India", "Walrus Handler")
    _company(db, f"{slug}b", "Berlin, Germany", "Walrus Handler")
    by_city = ats_search.index_jobs(db, "walrus handler", locations=["Bangalore"], country="India", max_age_hours=24,
                                    limit=5, detail_budget=0)
    by_country = ats_search.index_jobs(db, "walrus handler", locations=["India"], country="India", max_age_hours=24,
                                       limit=5, detail_budget=0)
    assert [j["location"] for j in by_city] == ["Bengaluru, India"] == [j["location"] for j in by_country]


def test_titles_with_no_other_names_yet_ask_the_ai_once(dispatched):
    title = f"Rare Title {uuid.uuid4().hex[:6]}"
    celery_tasks._queue_title_expansions([title, "Python Developer"], ["company_ats"], "u1")
    assert [c["args"][0] for c in dispatched if c["task"] == "job_finder.ats_expand_title"] == [title]


def test_no_ai_title_lookup_without_the_career_page_source(dispatched):
    celery_tasks._queue_title_expansions(["Another Rare Title"], ["indeed"], "u1")
    assert not [c for c in dispatched if c["task"] == "job_finder.ats_expand_title"]


# ---------------------------------------------------------------- J: counts per source

def test_run_meta_counts_jobs_per_source():
    run_id = pipeline_progress.create_run(user_id="u-qa9", total_keywords=2)
    pipeline_progress.add_source_counts(run_id=run_id, stage="raw", counts={"LinkedIn": 25, "Indeed": 80})
    pipeline_progress.add_source_counts(run_id=run_id, stage="raw", counts={"LinkedIn": 5})
    pipeline_progress.add_source_counts(run_id=run_id, stage="kept", counts={"Indeed": 3})
    assert pipeline_progress.get_run_meta(run_id=run_id)["sources"] == {
        "LinkedIn": {"raw": 30, "kept": 0}, "Indeed": {"raw": 80, "kept": 3}}


def test_kept_jobs_count_for_their_source(db, monkeypatch):
    run_id, _ = _process(db, monkeypatch, [_job("AI Engineer")], _params())
    assert pipeline_progress.get_run_meta(run_id=run_id)["sources"]["Adzuna"]["kept"] == 1


def test_prefilter_and_ai_are_counted_apart():
    run_id = pipeline_progress.create_run(user_id="u-qa9", total_keywords=1)
    pipeline_progress.add_counts(run_id=run_id, counts={"prefiltered": 4, "filtered": 2})
    counts = pipeline_progress.get_run_meta(run_id=run_id)["counts"]
    assert counts["prefiltered"] == 4 and counts["filtered"] == 2
