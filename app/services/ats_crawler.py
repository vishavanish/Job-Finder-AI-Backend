"""
app/services/ats_crawler.py
------------------------------
Keeps the ATS job index fresh (P6 plan §3.2–3.3).

  beat ats_tick -> claim_due() (conditional UPDATE, so two ticks never take
  the same company) -> one crawl_ats_company task each -> crawl_company():

    - per-host slot in Redis (ATS_HOST_CONCURRENCY at a time per ATS host;
      busy -> retried a minute later), conditional request with the stored
      ETag (304 = nothing to write);
    - jobs upserted by (company_id, external_id); unchanged rows untouched;
    - complete crawl: jobs no longer listed get closed_at; capped crawl
      (very large tenants): only jobs unseen for ATS_STALE_DAYS are closed;
    - 404 / redirect ATS_GONE_AFTER times in a row -> company switched off;
      other errors back off; a platform failing everywhere shows in
      ats_platforms health for the admin;
    - next crawl in ATS_HOT_REFRESH_HOURS while people search the company
      (hot_until) or ATS_COLD_REFRESH_HOURS otherwise, with jitter.

Index rows are short: title, location, snippet (≤ 300) — never the full
description (Supabase's free 500 MB would not hold 300k of them).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.ats import AtsCompany, AtsJob, AtsPlatformSetting
from app.providers.ats import platforms
from app.providers.ats.platforms import GoneError, ListResult
from app.providers.job_sources.base import SourceError, parse_date
from app.services import ats_registry
from app.services.job_dedupe import job_key, normalize_title

logger = logging.getLogger("job_finder_api.ats_crawler")

COUNTRY_CODES = {"in": "india", "us": "united states", "gb": "united kingdom", "uk": "united kingdom",
                 "de": "germany", "nl": "netherlands", "sg": "singapore", "ca": "canada", "au": "australia"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class HostBusy(Exception):
    """Every slot for this ATS host is taken; try again shortly."""


@contextmanager
def host_slot(host: str):
    """At most ATS_HOST_CONCURRENCY crawls per ATS host at once, across all workers."""
    r = get_redis()
    key = f"ats:host:{host}"
    n = r.incr(key)
    r.expire(key, 120)  # a crashed worker can't hold a slot for long
    try:
        if n > get_settings().ATS_HOST_CONCURRENCY:
            raise HostBusy(host)
        yield
    finally:
        r.decr(key)


def api_host(company: AtsCompany) -> str:
    return {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com",
            "smartrecruiters": "api.smartrecruiters.com", "workable": "apply.workable.com",
            "rippling": "ats.rippling.com", "gem": "api.gem.com"}.get(
        company.platform) or company.host or f"{company.slug}.{company.platform}"


def country_of(job: dict) -> str:
    c = (job.get("country") or "").strip()
    return COUNTRY_CODES.get(c.lower(), c)


def in_markets(job: dict, markets: tuple[str, ...]) -> bool:
    text = f"{job.get('location', '')} {country_of(job)}".lower()
    for m in markets:
        m = m.lower()
        if m == "remote" and (job.get("remote") or "remote" in text):
            return True
        if m != "remote" and m in text:
            return True
    return False


def cap_jobs(jobs: list[dict], max_jobs: int, markets: tuple[str, ...]) -> tuple[list[dict], bool]:
    """(jobs to index, complete?). Over the cap: target markets first, then the newest."""
    if len(jobs) <= max_jobs:
        return jobs, True
    newest = sorted(jobs, key=lambda j: parse_date(j.get("posted")) or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True)
    first = [j for j in newest if in_markets(j, markets)]
    rest = [j for j in newest if not in_markets(j, markets)]
    return (first + rest)[:max_jobs], False


def _hash(job: dict) -> str:
    keys = ("title", "location", "department", "url", "posted", "remote", "country")
    return hashlib.sha256(json.dumps([job.get(k) for k in keys] + [job.get("description", "")[:300]],
                                     default=str).encode()).hexdigest()


def _naive(dt: datetime | None) -> datetime | None:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt else None


def _row_fields(company: AtsCompany, job: dict) -> dict:
    title = job["title"]
    return {
        "title": title[:500], "title_norm": normalize_title(title)[:500], "location": job["location"][:255],
        "country": country_of(job)[:64], "remote": bool(job.get("remote")), "department": job["department"][:255],
        "url": job["url"] or company.careers_url, "snippet": platforms.snippet(job.get("description", "")),
        "job_key": job_key({"company": company.name, "title": title, "location": job["location"]})[:700],
        "content_hash": _hash(job), "posted_at": _naive(parse_date(job.get("posted"))),
    }


def index_full(db: Session) -> bool:
    """True once the index holds ATS_MAX_INDEXED_JOBS open jobs (checked at most every 5 minutes)."""
    r = get_redis()
    cached = r.get("ats:index_size")
    size = int(cached) if cached is not None else None
    if size is None:
        size = index_size(db)
        r.set("ats:index_size", size, ex=300)
    return size >= get_settings().ATS_MAX_INDEXED_JOBS


def upsert_jobs(db: Session, company: AtsCompany, jobs: list[dict], *, complete: bool, close: bool = True,
                new_rows: list | None = None) -> dict:
    """Writes one crawl's jobs; returns counts. complete=False never closes jobs it didn't see this time;
    close=False (a live text search, not a crawl) closes nothing.
    When the index is full (ATS_MAX_INDEXED_JOBS) known jobs are still refreshed but no new ones added."""
    now = _now()
    allow_new = not index_full(db)
    existing = {r.external_id: r for r in db.query(AtsJob).filter(AtsJob.company_id == company.id).all()}
    seen: set[str] = set()
    added = changed = 0
    for job in jobs:
        ext = job.get("external_id") or job.get("url")
        if not ext or not job.get("title") or ext in seen:
            continue
        seen.add(ext)
        fields = _row_fields(company, job)
        row = existing.get(ext)
        if row is None:
            if not allow_new:
                continue
            row = AtsJob(company_id=company.id, external_id=ext[:128], first_seen_at=now, last_seen_at=now, **fields)
            db.add(row)
            if new_rows is not None:
                new_rows.append(row)
            added += 1
            continue
        row.last_seen_at = now
        if row.closed_at is not None:
            row.closed_at = None
            changed += 1
        if row.content_hash != fields["content_hash"]:
            for k, v in fields.items():
                if k == "posted_at" and v is None:
                    continue
                setattr(row, k, v)
            changed += 1
    closed = 0
    stale_before = now - timedelta(days=get_settings().ATS_STALE_DAYS)
    for ext, row in existing.items():
        if not close or ext in seen or row.closed_at is not None:
            continue
        if complete or row.last_seen_at < stale_before:
            row.closed_at = now
            closed += 1
    db.flush()
    company.job_count = db.query(AtsJob).filter(AtsJob.company_id == company.id, AtsJob.closed_at.is_(None)).count()
    if not allow_new:
        logger.warning("ats index is full (%s jobs): new jobs of %s not added", get_settings().ATS_MAX_INDEXED_JOBS,
                       company.ref_key)
    return {"added": added, "changed": changed, "closed": closed}


def _next_crawl(company: AtsCompany, now: datetime) -> datetime:
    cfg = get_settings()
    hours = cfg.ATS_HOT_REFRESH_HOURS if company.hot_until and company.hot_until > now else cfg.ATS_COLD_REFRESH_HOURS
    if company.follower_count:
        hours = min(hours, cfg.ATS_FOLLOWED_REFRESH_HOURS)  # QA4: followed companies are checked more often
    return now + timedelta(hours=hours) + timedelta(minutes=random.uniform(-0.1, 0.1) * hours * 60)


def _platform_health(db: Session, platform: str, error: str | None) -> None:
    row = db.get(AtsPlatformSetting, platform)
    if row is None:
        row = AtsPlatformSetting(platform=platform, enabled=True, consecutive_failures=0)
        db.add(row)
    if error is None:
        row.consecutive_failures, row.last_success_at = 0, _now()
    else:
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_error_at, row.last_error = _now(), error[:300]


def fetch(company: AtsCompany) -> ListResult:
    cfg = get_settings()
    impl = platforms.get(company.platform)
    max_jobs = cfg.ATS_WORKDAY_MAX_JOBS if company.platform == "workday" else cfg.ATS_MAX_JOBS_PER_COMPANY
    result = impl.list_jobs(ats_registry.to_ref(company), etag=company.etag, max_jobs=max_jobs,
                            markets=cfg.ats_markets)
    if not result.not_modified:
        jobs, complete = cap_jobs(result.jobs, max_jobs, cfg.ats_markets)
        result.jobs, result.complete = jobs, result.complete and complete
    return result


def _board_name(company: AtsCompany, jobs: list[dict]) -> str | None:
    """The board's own name for a company still named after its slug (QA3 §D): one extra request,
    tried at most once per ATS_NAME_RETRY_DAYS per company. Never fails a crawl."""
    impl = platforms.get(company.platform)
    if impl is None:
        return None
    if not get_redis().set(f"ats:name_tried:{company.id}", "1", nx=True,
                           ex=get_settings().ATS_NAME_RETRY_DAYS * 86400):
        return None
    try:
        return impl.board_name(ats_registry.to_ref(company), jobs)
    except SourceError as e:
        logger.info("ats board name failed for %s: %s", company.ref_key, e)
        return None


def crawl_company(db: Session, company_id: str) -> dict:
    company = db.get(AtsCompany, company_id)
    if company is None or not company.active:
        return {"company_id": company_id, "skipped": "inactive"}
    if company.platform not in ats_registry.enabled_platforms(db):
        company.next_crawl_at = _now() + timedelta(hours=get_settings().ATS_COLD_REFRESH_HOURS)
        db.commit()
        return {"company_id": company_id, "skipped": "platform off"}
    now = _now()
    try:
        with host_slot(api_host(company)):
            result = fetch(company)
    except HostBusy:
        company.next_crawl_at = now + timedelta(seconds=60 + random.uniform(0, 60))
        db.commit()
        return {"company_id": company_id, "skipped": "host busy"}
    except GoneError as e:
        company.gone_count = (company.gone_count or 0) + 1
        company.last_crawled_at, company.last_error = now, str(e)[:300]
        if company.gone_count >= get_settings().ATS_GONE_AFTER:
            company.active = False
            db.query(AtsJob).filter(AtsJob.company_id == company.id, AtsJob.closed_at.is_(None)).update(
                {AtsJob.closed_at: now}, synchronize_session=False)
            company.job_count = 0
        else:
            company.next_crawl_at = now + timedelta(hours=6)
        db.commit()
        return {"company_id": company_id, "gone": company.gone_count, "active": company.active}
    except SourceError as e:
        return _failed(db, company, str(e))
    except Exception as e:  # noqa: BLE001 — a parser surprise on one company must not stop the crawler
        logger.exception("ats crawl failed for %s", company.ref_key)
        return _failed(db, company, f"{company.platform}: {type(e).__name__}")

    company.last_crawled_at = company.last_ok_at = now
    company.fail_count = company.gone_count = 0
    company.last_error = None
    company.next_crawl_at = _next_crawl(company, now)
    counts: dict = {"not_modified": True}
    added_rows: list[AtsJob] = []
    named_by_slug = company.origin != "admin" and company.name == ats_registry.display_name(
        ats_registry.to_ref(company))
    if not result.not_modified:
        company.etag = result.etag
        if named_by_slug:
            name = result.company_name or _board_name(company, result.jobs)
            if name:
                company.name = name.strip()[:255]
        counts = upsert_jobs(db, company, result.jobs, complete=result.complete, new_rows=added_rows)
    elif named_by_slug:
        if platforms.get(company.platform).name_from_page:
            name = _board_name(company, [])
            if name:
                company.name = name.strip()[:255]
        else:
            company.etag = None  # QA3 §D: the next crawl reads the full list, which carries the company name
    _platform_health(db, company.platform, None)
    db.commit()
    if company.follower_count and added_rows:
        _queue_follow_match(company.id, [r.id for r in added_rows])
    return {"company_id": company_id, **counts}


def _queue_follow_match(company_id: str, job_ids: list[str]) -> None:
    """QA4: new jobs at a followed company are matched against its followers on the maintenance queue."""
    from app.core.celery_tasks import ats_follow_match_task

    try:
        ats_follow_match_task.apply_async(args=[company_id, job_ids], queue=get_settings().maintenance_queue)
    except Exception:  # noqa: BLE001 — never fail a crawl for it
        logger.exception("could not queue the follow match for %s", company_id)


def _failed(db: Session, company: AtsCompany, error: str) -> dict:
    now = _now()
    company.fail_count = (company.fail_count or 0) + 1
    company.last_crawled_at, company.last_error = now, error[:300]
    company.next_crawl_at = now + timedelta(minutes=min(30 * 2 ** min(company.fail_count, 5), 24 * 60))
    _platform_health(db, company.platform, error)
    db.commit()
    return {"company_id": company.id, "error": error}


def claim_due(db: Session, limit: int | None = None) -> list[str]:
    """Companies due for a crawl, each claimed once even if two beats tick."""
    now = _now()
    limit = limit or get_settings().ATS_CRAWL_BATCH
    due = [c for (c,) in db.query(AtsCompany.id).filter(
        AtsCompany.active.is_(True), or_(AtsCompany.next_crawl_at.is_(None), AtsCompany.next_crawl_at <= now)
    ).order_by(AtsCompany.next_crawl_at).limit(limit).all()]
    later = now + timedelta(minutes=30)  # a lost task is retried after this
    claimed = []
    for company_id in due:
        n = db.execute(update(AtsCompany).where(
            AtsCompany.id == company_id,
            or_(AtsCompany.next_crawl_at.is_(None), AtsCompany.next_crawl_at <= now)).values(next_crawl_at=later)
        ).rowcount
        if n:
            claimed.append(company_id)
    db.commit()
    return claimed


def purge_closed(db: Session) -> int:
    before = _now() - timedelta(days=get_settings().ATS_CLOSED_RETENTION_DAYS)
    n = db.query(AtsJob).filter(AtsJob.closed_at.isnot(None), AtsJob.closed_at < before).delete(
        synchronize_session=False)
    db.commit()
    return n


def index_size(db: Session) -> int:
    return db.query(AtsJob.id).filter(AtsJob.closed_at.is_(None)).count()
