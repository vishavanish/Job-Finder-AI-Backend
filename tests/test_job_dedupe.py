from app.services.job_dedupe import dedupe, job_key


def test_key_ignores_case_punctuation_legal_suffix_and_region():
    linkedin = {"company": "Acme Pvt. Ltd.", "title": "Backend Engineer (Python)",
                "location": "Mumbai, Maharashtra, India"}
    indeed = {"company": "ACME", "title": "backend engineer - python", "location": "Mumbai"}
    assert job_key(linkedin) == job_key(indeed)


def test_different_city_is_a_different_job():
    a = {"company": "Acme", "title": "Backend Engineer", "location": "Mumbai"}
    b = {"company": "Acme", "title": "Backend Engineer", "location": "Pune"}
    assert job_key(a) != job_key(b)


def test_untitled_records_fall_back_to_url():
    a = {"url": "https://x.example/1"}
    b = {"url": "https://x.example/2"}
    assert job_key(a) != job_key(b)


def test_linkedin_copy_wins_even_when_seen_second():
    indeed = {"source": "Indeed", "company": "Acme", "title": "SRE", "location": "Pune",
              "description": "a much longer description than the other copy"}
    linkedin = {"source": "LinkedIn", "company": "Acme Inc", "title": "SRE", "location": "Pune, India",
                "description": "short"}
    assert dedupe([indeed, linkedin]) == [linkedin]


def test_longer_description_wins_between_non_linkedin_copies():
    short = {"source": "Indeed", "company": "Acme", "title": "SRE", "description": "short"}
    long = {"source": "Naukri", "company": "Acme", "title": "SRE", "description": "much longer text"}
    assert dedupe([short, long]) == [long]


def test_order_of_first_appearance_is_kept():
    jobs = [{"company": c, "title": "Dev"} for c in ("B", "A", "B", "C")]
    assert [j["company"] for j in dedupe(jobs)] == ["B", "A", "C"]
