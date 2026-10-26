"""
app/services/apply_options.py
--------------------------------
Which of the four apply buttons work for a job, and why not when they
don't (CLAUDE.md 7.7, P4 plan §3.2). Pure functions of the job dict, so
the feed, GET /jobs and the apply routes all give the same answer.

  manual  always — the user applies on the job's own page.
  fast    LinkedIn Easy Apply (a LinkedIn posting with no external apply
          link): fill the form, the user submits. Needs a browser on the
          user's own PC, so it is off in production until the local agent
          (P8) exists.
  auto    Greenhouse / Lever pages: the level-2 engine (P4b). Other hosted
          career forms (Ashby, SmartRecruiters, Workable, ...): the AI form
          reader (adapter "generic", P4c), which the user turns on in Apply
          settings. The engine's own checks (level, risk notice, opt-in,
          caps, duplicates) run on click.
  email   the job post itself gives an address to apply to.

The adapter is detected from `apply_url` (the company's own page behind a
board posting) first, then `url`.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.apply.sites import GENERIC_SOURCES, generic_host
from app.core.config import get_settings

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})(?![\w-])")
# Addresses that never take applications.
_NOT_FOR_APPLYING = re.compile(r"^(no-?reply|do-?not-?reply|privacy|abuse|unsubscribe|support|security|legal|"
                               r"dpo|gdpr|accommodations?|accessibility|webmaster|postmaster)[@.+-]", re.I)
_APPLY_HINT = re.compile(r"\b(apply|application|applications|cv|resume|résumé|send|email|e-mail|mail|"
                         r"hiring|careers?|jobs?|recruit\w*|talent|hr)\b", re.I)

ADAPTER_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "linkedin": "LinkedIn", "generic": "AI form reader"}


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def detect_adapter(url: str) -> str | None:
    """greenhouse | lever | linkedin | generic | None for one URL."""
    host = _host(url)
    if not host:
        return None
    if host.endswith("greenhouse.io") or "gh_jid=" in (url or ""):
        return "greenhouse"
    if host == "jobs.lever.co" or host.endswith(".lever.co"):
        return "lever"
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"
    if generic_host(url):
        return "generic"
    return None


def adapter_for(job: dict) -> str | None:
    """The company's own page wins: a LinkedIn posting that sends people to
    Greenhouse is a Greenhouse job (and not an Easy Apply one)."""
    direct = detect_adapter(job.get("apply_url") or "")
    if direct:
        return direct
    if job.get("apply_url") and (job.get("source") or "").lower() in GENERIC_SOURCES:
        return "generic"  # a career page on a custom host of a platform the AI form reader handles
    found = detect_adapter(job.get("url") or "")
    if found == "linkedin" and job.get("apply_url"):
        return None  # LinkedIn posting that applies on an unknown company site
    return found


def find_apply_email(description: str) -> str | None:
    """An address the post asks applicants to write to. Only addresses that
    appear near an apply-ish word count, and never no-reply/privacy ones."""
    text = description or ""
    for m in _EMAIL.finditer(text):
        address = m.group(1).rstrip(".")
        if _NOT_FOR_APPLYING.search(address):
            continue
        around = text[max(0, m.start() - 160): m.end() + 40]
        if _APPLY_HINT.search(around):
            return address
    return None


def _opt(available: bool, reason: str | None = None) -> dict:
    return {"available": available, "reason": None if available else reason}


def options(job: dict) -> dict:
    adapter = adapter_for(job)
    production = get_settings().ENVIRONMENT == "production"

    if adapter == "linkedin":
        fast = (_opt(False, "Fast apply for LinkedIn needs the local apply agent, which is not available yet.")
                if production else _opt(True))
    else:
        fast = _opt(False, "Fast apply works on LinkedIn Easy Apply jobs.")

    if adapter in ("greenhouse", "lever", "generic"):
        # Level, risk notice, the AI form reader opt-in, caps and duplicates are checked when the run starts
        # (apply_engine.create_run).
        auto = _opt(True)
    elif adapter == "linkedin":
        auto = _opt(False, "LinkedIn jobs are never submitted automatically — use Fast apply.")
    else:
        auto = _opt(False, "Auto apply works on Greenhouse, Lever and the career sites the AI form reader knows.")

    email = find_apply_email(job.get("description") or "")
    return {
        "adapter": adapter,
        "manual": _opt(True),
        "fast": fast,
        "auto": auto,
        "email": _opt(True) if email else _opt(False, "This job post gives no email address to apply to."),
        "email_to": email,
    }
