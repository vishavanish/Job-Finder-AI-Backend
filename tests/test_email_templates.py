"""HTML digest emails (QA7, 2026-10-31): content, escaping, link safety, and the multipart message."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.providers import email as email_provider
from app.services import email_templates

JOB = {"title": "Backend <Engineer>", "company": "Acme & Co", "location": "Pune", "url": "https://acme.example/jobs/1",
       "llm_score": 91.6, "llm_reason": "Python + FastAPI match", "early": True, "applicants": 12, "source": "LinkedIn",
       "match": {"skills_matched": ["python", "fastapi"], "skills_total": 3}}


def test_job_alert_digest_shows_each_job_with_its_signals(monkeypatch):
    monkeypatch.setattr(email_templates, "app_url", lambda path="": f"https://app.example{path}")
    html = email_templates.job_alert_digest("Backend — Pune", 75, [JOB, dict(JOB, title="Data Engineer", early=False,
                                                                          llm_score=72, url="https://x.example/2")])
    assert "2 new job matches for you" in html and "Backend — Pune" in html
    assert "92% match" in html and "New · under 24 h" in html and "2/3 skills" in html and "12 applicants" in html
    assert 'href="https://acme.example/jobs/1"' in html and 'href="https://app.example/alerts"' in html
    assert "Backend &lt;Engineer&gt;" in html and "Acme &amp; Co" in html  # escaped, never raw
    assert "<Engineer>" not in html
    assert html.lstrip().startswith("<!doctype html>") and 'name="color-scheme"' in html


def test_only_http_links_are_kept():
    bad = dict(JOB, url="javascript:alert(1)")
    html = email_templates.job_alert_digest("x", 70, [bad])
    assert "javascript:" not in html and "View job" not in html  # no button without a safe link


def test_company_digest_groups_roles_by_company(monkeypatch):
    monkeypatch.setattr(email_templates, "app_url", lambda path="": "")
    now = datetime(2026, 10, 31, 12)
    roles = [{"title": "Data Engineer", "company": "Kotak", "location": "Mumbai", "remote": False,
              "url": "https://k.example/1", "posted_at": now - timedelta(hours=3)},
             {"title": "SDE", "company": "Kotak", "location": "", "remote": True, "url": "https://k.example/2",
              "posted_at": now - timedelta(days=3)},
             {"title": "Backend", "company": "Razorpay <x>", "location": "Bengaluru", "remote": False,
              "url": "https://r.example/1", "posted_at": None}]
    html = email_templates.company_roles_digest(roles, now)
    assert "3 new roles at 2 companies" in html and "2 new roles" in html and "1 new role" in html
    assert "3 h ago" in html and "Remote · 3 days ago" in html and "Razorpay &lt;x&gt;" in html
    assert "Target companies" in html  # no app URL configured: the footer names the page instead


def test_smtp_sends_text_and_html_as_one_message(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(email_provider.smtplib, "SMTP", FakeSMTP)
    email_provider.SmtpEmailSender().send(to="a@b.co", subject="Hi", body="plain body", html="<p>rich</p>")
    msg = sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_content() for p in msg.iter_parts()}
    assert parts["text/plain"].strip() == "plain body" and "<p>rich</p>" in parts["text/html"]
