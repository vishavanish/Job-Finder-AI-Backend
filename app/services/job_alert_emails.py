"""
app/services/job_alert_emails.py
-----------------------------------
Jobs from job-alert emails in the user's own inbox (P5 plan §2.6) — the free
and safe Naukri path (research §6), plus LinkedIn / Indeed / Foundit /
Instahyre alerts. No AI: a job is a link to a known job page; its text is
the title, and the next short lines in the same block are the company and
(when it looks like one) the location. Blocks that can't be read are
skipped, never guessed.

EXPERIMENTAL: alert layouts aren't documented. Built against fixtures;
check it against a real alert email after connecting Gmail.
"""
from __future__ import annotations

import html as htmllib
import re
from urllib.parse import unquote

# Senders that send job alerts (a message from them is an alert only if it also links to job pages).
ALERT_SENDERS = re.compile(
    r"@(?:[\w-]+\.)*(naukri\.com|linkedin\.com|indeed\.com|indeedemail\.com|foundit\.in|monsterindia\.com|"
    r"instahyre\.com)\b", re.I)

_JOB_LINKS: list[tuple[str, re.Pattern, callable]] = [
    ("Naukri", re.compile(r"https?://(?:www\.)?naukri\.com/job-listings-[\w%-]+", re.I), lambda m: m.group(0)),
    ("LinkedIn", re.compile(r"https?://(?:[\w-]+\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I),
     lambda m: f"https://www.linkedin.com/jobs/view/{m.group(1)}"),
    ("Indeed", re.compile(r"https?://(?:[\w-]+\.)?indeed\.(?:com|co\.in)/[^\s\"'<>]*?[?&]jk=([0-9a-f]{8,})", re.I),
     lambda m: f"https://in.indeed.com/viewjob?jk={m.group(1)}"),
    ("Foundit", re.compile(r"https?://(?:www\.)?foundit\.in/job/[\w%-]+", re.I), lambda m: m.group(0)),
    ("Instahyre", re.compile(r"https?://(?:www\.)?instahyre\.com/job-[\w%-]+", re.I), lambda m: m.group(0)),
]
_GENERIC = re.compile(r"^(apply|apply now|view( job| details)?|see (job|more)|know more|easy apply|save|share|"
                      r"\d+ (new )?jobs?.*|view all.*|unsubscribe.*|.{0,2})$", re.I)
_BLOCK_TAGS = re.compile(r"</?(?:td|tr|p|div|li|table|br|h\d|section|span)\b[^>]*>", re.I)
_ANCHOR = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_LOCATION = re.compile(r"^[A-Z][A-Za-z.\- ]{1,30}(?:\s*[,/]\s*[A-Z][A-Za-z.\- ]{1,30}){0,4}$")
_NOT_LOCATION = re.compile(r"\b(yrs?|years?|lpa|lakh|salary|posted|ago|apply|days?|hours?|not disclosed)\b", re.I)


def _job_link(href: str) -> tuple[str, str] | None:
    """(source, canonical url) when `href` (or a tracking redirect wrapping it) is a job page."""
    for candidate in (href, unquote(href), unquote(unquote(href))):
        for source, pattern, canon in _JOB_LINKS:
            m = pattern.search(candidate)
            if m:
                return source, canon(m).split("?")[0] if source == "Naukri" else canon(m)
    return None


def _lines(html: str) -> list[str]:
    """Visible text as lines; anchors become '\\x00href\\x00text' lines."""
    def mark(m: re.Match) -> str:
        return f"\n\x00{htmllib.unescape(m.group(2))}\x00{_TAG.sub(' ', m.group(3))}\n"

    text = _ANCHOR.sub(mark, html or "")
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG.sub(" ", text)
    out = []
    for raw in htmllib.unescape(text).split("\n"):
        line = " ".join(raw.split())
        if line:
            out.append(line)
    return out


def is_alert(sender: str, subject: str, html: str, text: str) -> bool:
    if not ALERT_SENDERS.search(sender or ""):
        return False
    links = {u for _, u in filter(None, (_job_link(h) for h in re.findall(r"https?://[^\s\"'<>]+", html or text)))}
    alertish = re.search(r"job alert|jobs? for you|recommended|matching jobs|new jobs|jobs you may|apply now", subject or "",
                         re.I)
    return len(links) >= 2 or (len(links) == 1 and bool(alertish))


def parse(html: str, text: str = "") -> list[dict]:
    """[{source, title, company, location, url}] in the order they appear."""
    lines = _lines(html) if html else [" ".join(t.split()) for t in (text or "").splitlines() if t.strip()]
    jobs: dict[str, dict] = {}
    for i, line in enumerate(lines):
        if not line.startswith("\x00"):
            continue
        _, href, label = line.split("\x00", 2)
        found = _job_link(href)
        if not found:
            continue
        source, url = found
        label = " ".join(label.split())
        if url in jobs and jobs[url]["title"]:
            continue
        if not label or _GENERIC.match(label) or len(label) > 160:
            jobs.setdefault(url, {"source": source, "title": "", "company": "", "location": "", "url": url})
            continue
        following = []
        for nxt in lines[i + 1:i + 7]:
            if nxt.startswith("\x00"):
                break
            if not _GENERIC.match(nxt) and len(nxt) <= 80:
                following.append(nxt)
        company = following[0] if following else ""
        location = next((x for x in following[1:] if _LOCATION.match(x) and not _NOT_LOCATION.search(x)), "")
        jobs[url] = {"source": source, "title": label[:500], "company": company[:255], "location": location[:255],
                     "url": url}
    return [j for j in jobs.values() if j["title"]]
