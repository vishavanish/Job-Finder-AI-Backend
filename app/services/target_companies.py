"""
app/services/target_companies.py
-----------------------------------
Target companies (QA4): a user follows the companies they want to work for
and is told first when one posts a matching role.

  - follow / unfollow a registry company; follow by name (the QA3 resolver
    looks for the career page; the follow waits as "searching" /
    "not_found" and attaches itself when the company turns up later);
  - followed companies refresh every ATS_FOLLOWED_REFRESH_HOURS
    (`AtsCompany.follower_count`, ats_crawler._next_crawl);
  - a crawl that adds jobs to a followed company calls match_new_jobs():
    jobs whose title matches the follower's titles (the ATS title matcher,
    synonyms included, no AI) in their places become AtsFollowHit rows;
  - digest(): instant (at most hourly) or daily (ATS_FOLLOW_DIGEST_HOUR
    local) email of un-emailed hits; "off" keeps them on the page only;
  - suggestions(): companies with the most open matching roles in the
    user's places, plus companies of jobs they saved or applied to.

Timestamps are naive UTC, like the rest of the ATS tables.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.application import Application
from app.models.ats import AtsCompany, AtsFollow, AtsFollowHit, AtsFollowSettings, AtsJob, AtsProbe
from app.models.profile import JobPreference, SearchProfile
from app.models.user import User
from app.providers.ats import platforms
from app.services import ats_search, email_templates, entitlements
from app.services.job_dedupe import normalize_company

logger = logging.getLogger("job_finder_api.target_companies")

ALERT_MODES = ("instant", "daily", "off")
MAX_EMAIL_ROLES = 20
SUGGESTION_TTL = 24 * 3600


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- settings

def get_settings_row(db: Session, user_id: str) -> AtsFollowSettings:
    row = db.get(AtsFollowSettings, user_id)
    if row is None:
        row = AtsFollowSettings(user_id=user_id, alert_mode="daily", titles=[], places=[], timezone="Asia/Kolkata")
    return row


def default_titles(db: Session, user_id: str) -> list[str]:
    profile = (db.query(SearchProfile).filter(SearchProfile.user_id == user_id)
               .order_by(SearchProfile.is_default.desc(), SearchProfile.created_at).first())
    titles = list(profile.titles or []) if profile else []
    if not titles:
        prefs = db.query(JobPreference).filter(JobPreference.user_id == user_id).first()
        titles = list(prefs.titles or []) if prefs else []
    return [t for t in titles if isinstance(t, str) and t.strip()][:10]


def default_places(db: Session, user_id: str) -> list[str]:
    prefs = db.query(JobPreference).filter(JobPreference.user_id == user_id).first()
    places = [p for p in (prefs.locations or [] if prefs else []) if isinstance(p, str) and p.strip()]
    if places and not any("remote" in p.lower() for p in places):
        places.append("Remote")
    return places[:10]


def effective_titles(db: Session, row: AtsFollowSettings) -> list[str]:
    return list(row.titles or []) or default_titles(db, row.user_id)


def effective_places(db: Session, row: AtsFollowSettings) -> list[str]:
    return list(row.places or []) or default_places(db, row.user_id)


def settings_out(db: Session, row: AtsFollowSettings) -> dict:
    return {"alert_mode": row.alert_mode, "titles": list(row.titles or []), "places": list(row.places or []),
            "timezone": row.timezone, "default_titles": default_titles(db, row.user_id),
            "default_places": default_places(db, row.user_id)}


def save_settings(db: Session, user_id: str, *, alert_mode: str | None, titles: list[str] | None,
                  places: list[str] | None, tz: str | None) -> AtsFollowSettings:
    row = get_settings_row(db, user_id)
    if alert_mode is not None:
        if alert_mode not in ALERT_MODES:
            raise HTTPException(422, f"alert_mode must be one of {', '.join(ALERT_MODES)}")
        row.alert_mode = alert_mode
    if titles is not None:
        row.titles = [t.strip()[:120] for t in titles if t and t.strip()][:10]
    if places is not None:
        row.places = [p.strip()[:80] for p in places if p and p.strip()][:10]
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Unknown time zone.") from None
        row.timezone = tz
    db.merge(row)
    db.commit()
    return db.get(AtsFollowSettings, user_id)


# ---------------------------------------------------------------- matching

def title_phrases(titles: list[str]) -> list[list[str]]:
    """Each title with its synonyms, as word lists (a job matches when its title has every word of one)."""
    out: list[list[str]] = []
    for title in titles:
        phrases, _ = ats_search.expansions(title)
        for phrase in phrases:
            words = phrase.split()
            if words and words not in out:
                out.append(words)
    return out


def title_matches(title_norm: str, phrases: list[list[str]]) -> bool:
    if not phrases:
        return True  # no titles set: every role counts
    return any(all(w in title_norm for w in words) for words in phrases)


def place_matches(job: AtsJob, places: list[str]) -> bool:
    if not places:
        return True
    loc = f"{job.location} {job.country}".lower()
    for place in places:
        p = place.lower().strip()
        if p == "remote" and job.remote:
            return True
        city = p.split(",")[0].strip()
        if city and city in loc:
            return True
    return False


def _criteria(db: Session, user_id: str, cache: dict) -> tuple[list[list[str]], list[str]]:
    if user_id not in cache:
        row = get_settings_row(db, user_id)
        cache[user_id] = (title_phrases(effective_titles(db, row)), effective_places(db, row))
    return cache[user_id]


def match_new_jobs(db: Session, company_id: str, job_ids: list[str]) -> int:
    """New jobs at a followed company -> hits for each follower whose titles and places they match."""
    if not job_ids:
        return 0
    followers = [u for (u,) in db.query(AtsFollow.user_id).filter(
        AtsFollow.company_id == company_id, AtsFollow.status == "following").all()]
    if not followers:
        return 0
    jobs = db.query(AtsJob).filter(AtsJob.id.in_(job_ids), AtsJob.closed_at.is_(None)).all()
    cache: dict = {}
    added = 0
    for user_id in followers:
        phrases, places = _criteria(db, user_id, cache)
        existing = {j for (j,) in db.query(AtsFollowHit.ats_job_id).filter(
            AtsFollowHit.user_id == user_id, AtsFollowHit.ats_job_id.in_(job_ids)).all()}
        for job in jobs:
            if job.id in existing or not title_matches(job.title_norm, phrases) or not place_matches(job, places):
                continue
            db.add(AtsFollowHit(user_id=user_id, company_id=company_id, ats_job_id=job.id))
            added += 1
    db.commit()
    return added


# ---------------------------------------------------------------- follow / unfollow

def _recount(db: Session, company_id: str | None) -> None:
    if not company_id:
        return
    n = db.query(func.count(AtsFollow.id)).filter(AtsFollow.company_id == company_id,
                                                   AtsFollow.status == "following").scalar() or 0
    company = db.get(AtsCompany, company_id)
    if company is not None:
        company.follower_count = n
        if n and (company.next_crawl_at is None or company.next_crawl_at >
                  _now() + timedelta(hours=get_settings().ATS_FOLLOWED_REFRESH_HOURS)):
            company.next_crawl_at = _now()  # check it soon, then every few hours


def _check_limit(db: Session, user_id: str) -> None:
    n = db.query(func.count(AtsFollow.id)).filter(AtsFollow.user_id == user_id).scalar() or 0
    limit = get_settings().TARGET_COMPANIES_MAX_PER_USER
    if n >= limit:
        raise HTTPException(409, f"You can follow up to {limit} companies. Unfollow one to add another.")


def follow(db: Session, user_id: str, company: AtsCompany) -> AtsFollow:
    row = db.query(AtsFollow).filter(AtsFollow.user_id == user_id, AtsFollow.company_id == company.id).first()
    if row is not None:
        return row
    _check_limit(db, user_id)
    row = AtsFollow(user_id=user_id, company_id=company.id, name=company.name[:255],
                    name_norm=normalize_company(company.name)[:255], status="following")
    db.add(row)
    db.flush()
    _recount(db, company.id)
    db.commit()
    _forget_suggestions(user_id)
    return row


def unfollow(db: Session, user_id: str, follow_id: str | None = None, company_id: str | None = None) -> None:
    q = db.query(AtsFollow).filter(AtsFollow.user_id == user_id)
    q = q.filter(AtsFollow.id == follow_id) if follow_id else q.filter(AtsFollow.company_id == company_id)
    row = q.first()
    if row is None:
        raise HTTPException(404, "You don't follow this company.")
    cid = row.company_id
    if cid:
        db.query(AtsFollowHit).filter(AtsFollowHit.user_id == user_id, AtsFollowHit.company_id == cid).delete()
    db.delete(row)
    db.flush()
    _recount(db, cid)
    db.commit()
    _forget_suggestions(user_id)


def find_by_name(db: Session, name: str) -> AtsCompany | None:
    """A registry company for a typed name: the resolver's earlier answer, else a matching registry name."""
    from app.services.ats_resolver import names_match

    norm = normalize_company(name)
    probe = db.get(AtsProbe, norm)
    if probe is not None and probe.company_id:
        company = db.get(AtsCompany, probe.company_id)
        if company is not None and company.active:
            return company
    first = norm.split()[0] if norm else ""
    if not first:
        return None
    rows = db.query(AtsCompany).filter(AtsCompany.active.is_(True),
                                       func.lower(AtsCompany.name).like(f"%{first}%")).limit(200).all()
    best = [c for c in rows if names_match(name, c.name)]
    return max(best, key=lambda c: c.job_count) if best else None


def follow_by_name(db: Session, user_id: str, name: str) -> tuple[AtsFollow, bool]:
    """(follow, needs_lookup). Known company -> followed at once; otherwise a "searching" follow that the
    caller hands to the resolver task."""
    name = name.strip()[:255]
    norm = normalize_company(name)[:255]
    if not norm:
        raise HTTPException(422, "Type a company name.")
    company = find_by_name(db, name)
    if company is not None:
        return follow(db, user_id, company), False
    row = db.query(AtsFollow).filter(AtsFollow.user_id == user_id, AtsFollow.company_id.is_(None),
                                     AtsFollow.name_norm == norm).first()
    if row is None:
        _check_limit(db, user_id)
        row = AtsFollow(user_id=user_id, company_id=None, name=name, name_norm=norm, status="searching")
        db.add(row)
    else:
        row.status = "searching"
    db.commit()
    return row, True


def attach_found(db: Session, name_norm: str, company: AtsCompany) -> int:
    """The resolver found a company: follows waiting on that name now follow it (QA4 §6)."""
    rows = db.query(AtsFollow).filter(AtsFollow.company_id.is_(None), AtsFollow.name_norm == name_norm).all()
    attached = 0
    for row in rows:
        if db.query(AtsFollow.id).filter(AtsFollow.user_id == row.user_id,
                                         AtsFollow.company_id == company.id).first():
            db.delete(row)  # already following it another way
            continue
        row.company_id, row.status, row.name = company.id, "following", company.name[:255]
        attached += 1
    db.flush()
    _recount(db, company.id)
    db.commit()
    return attached


def lookup_name(session_factory, follow_id: str) -> dict:
    """Celery: resolve a followed name with the QA3 resolver, then attach or mark it not found."""
    from app.services import ats_resolver

    db = session_factory()
    try:
        row = db.get(AtsFollow, follow_id)
        if row is None or row.company_id:
            return {"status": "done"}
        name, norm = row.name, row.name_norm
        db.query(AtsProbe).filter(AtsProbe.name_norm == norm, AtsProbe.company_id.is_(None)).delete()
        db.commit()
    finally:
        db.close()
    ats_resolver.resolve(session_factory, [{"company": name}], force=True)
    db = session_factory()
    try:
        row = db.get(AtsFollow, follow_id)
        if row is not None and row.company_id is None:
            row.status = "not_found"
            db.commit()
            return {"status": "not_found"}
        return {"status": "following"}
    finally:
        db.close()


# ---------------------------------------------------------------- reading

def _open_jobs(db: Session, company_ids: list[str]) -> dict[str, list[AtsJob]]:
    out: dict[str, list[AtsJob]] = {c: [] for c in company_ids}
    if not company_ids:
        return out
    cutoff = _now() - timedelta(days=get_settings().ATS_MAX_POSTING_AGE_DAYS or 3650)
    for job in db.query(AtsJob).filter(AtsJob.company_id.in_(company_ids), AtsJob.closed_at.is_(None),
                                       func.coalesce(AtsJob.posted_at, AtsJob.first_seen_at) >= cutoff).all():
        out[job.company_id].append(job)
    return out


def following(db: Session, user_id: str) -> list[dict]:
    rows = db.query(AtsFollow).filter(AtsFollow.user_id == user_id).order_by(AtsFollow.created_at.desc()).all()
    phrases, places = _criteria(db, user_id, {})
    company_ids = [r.company_id for r in rows if r.company_id]
    companies = {c.id: c for c in db.query(AtsCompany).filter(AtsCompany.id.in_(company_ids)).all()} \
        if company_ids else {}
    jobs = _open_jobs(db, company_ids)
    unseen = dict(db.query(AtsFollowHit.company_id, func.count(AtsFollowHit.id)).join(
        AtsJob, AtsJob.id == AtsFollowHit.ats_job_id).filter(
        AtsFollowHit.user_id == user_id, AtsFollowHit.seen_at.is_(None), AtsJob.closed_at.is_(None)
    ).group_by(AtsFollowHit.company_id).all()) if company_ids else {}
    out = []
    for r in rows:
        c = companies.get(r.company_id) if r.company_id else None
        matching = [j for j in jobs.get(r.company_id, []) if title_matches(j.title_norm, phrases)
                    and place_matches(j, places)] if c else []
        newest = max((j.posted_at or j.first_seen_at for j in matching), default=None)
        impl = platforms.get(c.platform) if c else None
        out.append({
            "id": r.id, "status": r.status, "name": c.name if c else r.name, "company_id": r.company_id,
            "platform": c.platform if c else None, "platform_name": impl.name if impl else None,
            "careers_url": c.careers_url if c else None, "open_jobs": c.job_count if c else 0,
            "open_matching": len(matching), "new_count": unseen.get(r.company_id, 0),
            "latest_posted_at": newest, "last_refreshed_at": c.last_ok_at if c else None,
            "followed_at": r.created_at,
        })
    return out


def new_roles(db: Session, user_id: str, *, company_id: str | None = None, limit: int = 50) -> list[dict]:
    q = db.query(AtsFollowHit, AtsJob, AtsCompany).join(AtsJob, AtsJob.id == AtsFollowHit.ats_job_id).join(
        AtsCompany, AtsCompany.id == AtsFollowHit.company_id).filter(
        AtsFollowHit.user_id == user_id, AtsJob.closed_at.is_(None))
    if company_id:
        q = q.filter(AtsFollowHit.company_id == company_id)
    out = []
    for hit, job, company in q.order_by(AtsFollowHit.created_at.desc()).limit(limit).all():
        out.append({"id": hit.id, "job_id": job.id, "title": job.title, "location": job.location,
                    "remote": job.remote, "url": job.url, "company_id": company.id, "company": company.name,
                    "platform": company.platform, "posted_at": job.posted_at or job.first_seen_at,
                    "found_at": hit.created_at, "seen": hit.seen_at is not None})
    return out


def mark_seen(db: Session, user_id: str, company_id: str | None = None) -> int:
    now = _now()
    q = db.query(AtsFollowHit).filter(AtsFollowHit.user_id == user_id, AtsFollowHit.seen_at.is_(None))
    f = db.query(AtsFollow).filter(AtsFollow.user_id == user_id)
    if company_id:
        q = q.filter(AtsFollowHit.company_id == company_id)
        f = f.filter(AtsFollow.company_id == company_id)
    n = q.update({AtsFollowHit.seen_at: now}, synchronize_session=False)
    f.update({AtsFollow.last_seen_at: now}, synchronize_session=False)
    db.commit()
    return n


def followed_ids(db: Session, user_id: str) -> set[str]:
    return {c for (c,) in db.query(AtsFollow.company_id).filter(AtsFollow.user_id == user_id,
                                                               AtsFollow.company_id.isnot(None)).all()}


def followed_names(db: Session, user_id: str) -> list[str]:
    """Normalised names of followed companies, for the ★ on Jobs (QA4 §2)."""
    return sorted({n for (n,) in db.query(AtsFollow.name_norm).filter(
        AtsFollow.user_id == user_id, AtsFollow.status == "following").all()})


# ---------------------------------------------------------------- suggestions

def _forget_suggestions(user_id: str) -> None:
    get_redis().delete(f"ats:suggest:{user_id}")


def suggestions(db: Session, user_id: str, limit: int = 12) -> list[dict]:
    r = get_redis()
    key = f"ats:suggest:{user_id}"
    cached = r.get(key)
    if cached:
        return json.loads(cached)
    row = get_settings_row(db, user_id)
    titles, places = effective_titles(db, row)[:3], effective_places(db, row)
    taken = followed_ids(db, user_id)
    scores: dict[str, dict] = {}
    for title in titles:
        result = ats_search.search(db, user_id, title, location=places, posted_within_days=30)
        for g in result["groups"]:
            if g["company_id"] in taken:
                continue
            s = scores.setdefault(g["company_id"], {
                "company_id": g["company_id"], "name": g["name"], "platform": g["platform"],
                "platform_name": g["platform_name"], "open_matching": 0, "reason": "hiring for your titles"})
            s["open_matching"] += g["matching_jobs"]
    # Companies of jobs the user saved or applied to.
    names = {normalize_company(c) for (c,) in db.query(Application.company).filter(
        Application.user_id == user_id).limit(200).all() if c}
    if names:
        for c in db.query(AtsCompany).filter(AtsCompany.active.is_(True)).all():
            if c.id not in taken and c.id not in scores and normalize_company(c.name) in names:
                impl = platforms.get(c.platform)
                scores[c.id] = {"company_id": c.id, "name": c.name, "platform": c.platform,
                                "platform_name": impl.name if impl else c.platform, "open_matching": 0,
                                "reason": "you applied there"}
    out = sorted(scores.values(), key=lambda s: s["open_matching"], reverse=True)[:limit]
    r.set(key, json.dumps(out), ex=SUGGESTION_TTL)
    return out


# ---------------------------------------------------------------- digest email

def _local(now: datetime, tz: str) -> datetime:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Asia/Kolkata")
    return now.replace(tzinfo=timezone.utc).astimezone(zone)


def due(row: AtsFollowSettings, now: datetime) -> bool:
    if row.alert_mode == "instant":
        return row.last_digest_at is None or row.last_digest_at <= now - timedelta(hours=1)
    if row.alert_mode == "daily":
        local = _local(now, row.timezone)
        if local.hour < get_settings().ATS_FOLLOW_DIGEST_HOUR:
            return False
        return row.last_digest_at is None or _local(row.last_digest_at, row.timezone).date() < local.date()
    return False


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return ""
    hours = max(0, int((now - when).total_seconds() // 3600))
    if hours < 1:
        return "posted just now"
    if hours < 48:
        return f"posted {hours} h ago"
    return f"posted {hours // 24} days ago"


def digest_text(roles: list[dict], now: datetime) -> tuple[str, str]:
    cfg = get_settings()
    companies = sorted({r["company"] for r in roles})
    subject = (f"New at {companies[0]}: {roles[0]['title']}" if len(roles) == 1 else
               f"{len(roles)} new roles at companies you follow")
    lines = ["New roles at companies you follow:", ""]
    for r in roles:
        where = r["location"] or ("Remote" if r["remote"] else "")
        lines += [f"New at {r['company']}: {r['title']}" + (f" ({where})" if where else "")
                  + (f" — {_ago(r['posted_at'], now)}" if r["posted_at"] else ""), f"   {r['url']}", ""]
    page = f"{cfg.APP_PUBLIC_URL.rstrip('/')}/ats-targets" if cfg.APP_PUBLIC_URL else "Job Finder AI → Target companies"
    lines += [f"See every followed company and change these emails: {page}"]
    return subject, "\n".join(lines)


def digest(db: Session, *, send=None, now: datetime | None = None) -> dict:
    """Beat: emails each due user their un-emailed new roles. Returns counts."""
    from app.providers.email import get_email_sender

    now = now or _now()
    counts = {"users": 0, "emails": 0, "roles": 0, "skipped": 0}
    user_ids = [u for (u,) in db.query(AtsFollowHit.user_id).join(AtsJob, AtsJob.id == AtsFollowHit.ats_job_id).filter(
        AtsFollowHit.emailed_at.is_(None), AtsFollowHit.seen_at.is_(None), AtsJob.closed_at.is_(None)
    ).distinct().all()]
    for user_id in user_ids:
        counts["users"] += 1
        row = get_settings_row(db, user_id)
        if not due(row, now):
            continue
        user = db.get(User, user_id)
        if user is None or not user.is_active or not entitlements.effective(db, user_id, "ats_discovery").enabled:
            counts["skipped"] += 1
            continue
        hits = db.query(AtsFollowHit, AtsJob, AtsCompany).join(AtsJob, AtsJob.id == AtsFollowHit.ats_job_id).join(
            AtsCompany, AtsCompany.id == AtsFollowHit.company_id).filter(
            AtsFollowHit.user_id == user_id, AtsFollowHit.emailed_at.is_(None), AtsFollowHit.seen_at.is_(None),
            AtsJob.closed_at.is_(None)).order_by(AtsFollowHit.created_at.desc()).limit(MAX_EMAIL_ROLES).all()
        if not hits:
            continue
        roles = [{"title": j.title, "company": c.name, "location": j.location, "remote": j.remote, "url": j.url,
                  "posted_at": j.posted_at or j.first_seen_at} for _, j, c in hits]
        subject, body = digest_text(roles, now)
        html = email_templates.company_roles_digest(roles, now)
        try:
            (send or get_email_sender().send)(to=user.email, subject=subject, body=body, html=html)
        except Exception:  # noqa: BLE001 — try again next tick
            logger.exception("target companies digest failed for %s", user_id)
            counts["skipped"] += 1
            continue
        for hit, _, _ in hits:
            hit.emailed_at = now
        row.last_digest_at = now
        db.merge(row)
        db.commit()
        counts["emails"] += 1
        counts["roles"] += len(roles)
    return counts


def purge(db: Session) -> int:
    cutoff = _now() - timedelta(days=get_settings().ATS_FOLLOW_HIT_RETENTION_DAYS)
    n = db.query(AtsFollowHit).filter(AtsFollowHit.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return n


def company_followed(db: Session, user_id: str, company_ids: list[str]) -> set[str]:
    if not company_ids:
        return set()
    return {c for (c,) in db.query(AtsFollow.company_id).filter(AtsFollow.user_id == user_id,
                                                               AtsFollow.company_id.in_(company_ids)).all()}

