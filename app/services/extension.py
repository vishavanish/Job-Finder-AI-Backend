"""
app/services/extension.py
---------------------------
Browser extension "Job Finder capture" (plan docs/plans/P8a-browser-extension.md, E1).

The user's own browser saves the jobs on a job-search page they opened; nothing here fetches those sites,
so no captcha or block is ever worked around (CLAUDE.md §12).

Pairing: the signed-in web app asks for a 6-digit code (Redis, PAIR_CODE_SECONDS, single use, hashed) →
the extension trades it for a capture-only token (`jfx_…`, only its SHA-256 stored). The token works only
on POST /extension/captures (and GET /extension/me); it is listed and revocable in Settings.

Captures are validated hard: a known site, every job URL https on that site's hosts, short plain-text
fields, ≤ MAX_JOBS per call. They feed the "Saved from your browser" search source (kind `capture`).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.redis_client import get_redis
from app.models.extension import CapturedJob, ExtensionToken
from app.models.user import User
from app.providers.job_sources.base import strip_html

PAIR_CODE_SECONDS = 600
TOKEN_PREFIX = "jfx_"
MAX_JOBS = 100
MAX_TOKENS_PER_USER = 5
CAPTURE_RETENTION_DAYS = 30

# site -> (source name shown on jobs, allowed hosts). Job links must be on these hosts.
SITES: dict[str, tuple[str, tuple[str, ...]]] = {
    "naukri": ("Naukri", ("naukri.com",)),
    "foundit": ("Foundit", ("foundit.in", "foundit.com", "monsterindia.com")),
    "glassdoor": ("Glassdoor", ("glassdoor.com", "glassdoor.co.in", "glassdoor.co.uk", "glassdoor.ca")),
    "linkedin": ("LinkedIn", ("linkedin.com",)),
    "indeed": ("Indeed", ("indeed.com", "indeed.co.in", "indeed.co.uk")),
}
_SPACE = re.compile(r"\s+")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, "details": {}})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pair_key(code: str) -> str:
    return "ext_pair:" + _hash(code)


# ---------------------------------------------------------------- pairing and tokens

def create_pair_code(user_id: str) -> tuple[str, int]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    get_redis().set(_pair_key(code), user_id, ex=PAIR_CODE_SECONDS)
    return code, PAIR_CODE_SECONDS


def exchange_code(db: Session, code: str, name: str | None = None) -> tuple[str, ExtensionToken]:
    code = (code or "").strip()
    user_id = get_redis().getdel(_pair_key(code)) if re.fullmatch(r"\d{6}", code) else None
    if not user_id:
        raise _error(400, "pair_code_invalid", "That code is wrong or has expired. Get a new one in Settings.")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _error(403, "account_disabled", "This account is disabled.")
    active = db.query(ExtensionToken).filter(ExtensionToken.user_id == user_id,
                                             ExtensionToken.revoked_at.is_(None)).order_by(ExtensionToken.created_at)
    rows = active.all()
    for old in rows[: max(0, len(rows) - MAX_TOKENS_PER_USER + 1)]:  # keep the newest few connections
        old.revoked_at = _now()
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ExtensionToken(user_id=user_id, token_hash=_hash(raw),
                         name=(_SPACE.sub(" ", name or "").strip() or "Browser extension")[:120])
    db.add(row)
    db.commit()
    return raw, row


def user_for_token(db: Session, raw: str | None) -> tuple[User, ExtensionToken]:
    if not raw or not raw.startswith(TOKEN_PREFIX) or len(raw) > 200:
        raise _error(401, "extension_token_invalid", "Connect the extension again from Settings.")
    digest = _hash(raw)
    row = db.query(ExtensionToken).filter(ExtensionToken.token_hash == digest).first()
    if row is None or row.revoked_at is not None or not hmac.compare_digest(row.token_hash, digest):
        raise _error(401, "extension_token_invalid", "Connect the extension again from Settings.")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise _error(403, "account_disabled", "This account is disabled.")
    row.last_used_at = _now()
    db.commit()
    return user, row


def list_tokens(db: Session, user_id: str) -> list[ExtensionToken]:
    return (db.query(ExtensionToken).filter(ExtensionToken.user_id == user_id, ExtensionToken.revoked_at.is_(None))
            .order_by(ExtensionToken.created_at.desc()).all())


def has_connection(db: Session, user_id: str) -> bool:
    return db.query(ExtensionToken.id).filter(ExtensionToken.user_id == user_id,
                                              ExtensionToken.revoked_at.is_(None)).first() is not None


def revoke(db: Session, user_id: str, token_id: str) -> None:
    row = db.query(ExtensionToken).filter(ExtensionToken.id == token_id, ExtensionToken.user_id == user_id).first()
    if row is None or row.revoked_at is not None:
        raise _error(404, "not_found", "That connection doesn't exist.")
    row.revoked_at = _now()
    db.commit()


# ---------------------------------------------------------------- captures

def _text(value, limit: int) -> str:
    return _SPACE.sub(" ", strip_html(str(value or ""))).strip()[:limit]


def _on_site(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in hosts)


def store_captures(db: Session, user_id: str, site: str, page_url: str, jobs: list[dict]) -> dict:
    if site not in SITES:
        raise _error(422, "site_unsupported", f"Saving from '{site}' isn't supported yet.")
    source, hosts = SITES[site]
    if not _on_site(page_url or "", hosts):
        raise _error(422, "page_not_on_site", "The page isn't on the site it says it is.")
    if len(jobs) > MAX_JOBS:
        raise _error(422, "too_many_jobs", f"At most {MAX_JOBS} jobs per save.")
    saved = updated = skipped = 0
    now = _now()
    seen: set[str] = set()
    for raw in jobs:
        url = str(raw.get("url") or "").strip()[:2000]
        title = _text(raw.get("title"), 300)
        if not title or url in seen or not _on_site(url, hosts):
            skipped += 1
            continue
        seen.add(url)
        fields = {"site": site, "source": source, "title": title, "company": _text(raw.get("company"), 255),
                  "location": _text(raw.get("location"), 255), "description": _text(raw.get("description"), 5000),
                  "posted": _text(raw.get("posted"), 64), "captured_at": now}
        row = db.query(CapturedJob).filter(CapturedJob.user_id == user_id, CapturedJob.url == url).first()
        if row is not None:
            for key, value in fields.items():
                if value or key == "captured_at":
                    setattr(row, key, value)
            updated += 1
            continue
        try:
            with db.begin_nested():  # a save from another tab at the same moment: keep the rest
                db.add(CapturedJob(user_id=user_id, url=url, **fields))
            saved += 1
        except IntegrityError:
            skipped += 1
    cutoff = now - timedelta(days=CAPTURE_RETENTION_DAYS)
    db.query(CapturedJob).filter(CapturedJob.user_id == user_id, CapturedJob.captured_at < cutoff).delete(
        synchronize_session=False)
    db.commit()
    return {"saved": saved, "updated": updated, "skipped": skipped,
            "total": db.query(CapturedJob).filter(CapturedJob.user_id == user_id).count()}


def recent_captured_jobs(db: Session, user_id: str, max_age_hours: int) -> list[dict]:
    """Saved-from-browser jobs for the 'Saved from your browser' search source (newest first)."""
    since = _now() - timedelta(hours=max(max_age_hours, 24 * 7))  # a save stays useful for a week
    rows = (db.query(CapturedJob).filter(CapturedJob.user_id == user_id, CapturedJob.captured_at >= since)
            .order_by(CapturedJob.captured_at.desc()).limit(500).all())
    return [{"source": r.source, "title": r.title, "company": r.company, "location": r.location, "url": r.url,
             "description": r.description, "posted": r.posted or r.captured_at.replace(tzinfo=timezone.utc).isoformat()}
            for r in rows]


def stats(db: Session, user_id: str) -> dict:
    since = _now() - timedelta(days=1)
    q = db.query(CapturedJob).filter(CapturedJob.user_id == user_id)
    return {"total": q.count(), "today": q.filter(CapturedJob.captured_at >= since).count()}
