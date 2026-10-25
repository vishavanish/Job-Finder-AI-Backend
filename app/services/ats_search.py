"""
app/services/ats_search.py
-----------------------------
Title search over the ATS job index, grouped by company (P6 plan §3.4–3.5).

  1. normalise the title (case, seniority words, common abbreviations);
  2. expand it: built-in synonym groups first (works under Free tools
     only), plus an AI expansion cached in Redis for 30 days. The AI call
     never runs in the request: on a miss a background task fills the
     cache for the next search;
  3. match job titles: Postgres full-text on the generated `tsv` column,
     title words only (weight A), ranked by ts_rank then recency; SQLite
     (dev/tests) matches every word of an expansion in title_norm;
  4. filter (platform, location, remote, posted within, hide applied),
     group by company, page of 25 companies, cached ATS_SEARCH_CACHE_SECONDS;
  5. fewer than ATS_MIN_COMPANIES companies -> a live top-up task
     (topup()) searches / refreshes stale companies; the client polls it
     and searches again.

index_jobs() is the same match for the pipeline's `company_ats` source.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, func, or_, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.application import Application
from app.models.ats import AtsCompany, AtsJob
from app.providers.ats import platforms
from app.providers.job_sources.base import SourceError, geo_allows
from app.services import ats_crawler, ats_registry

logger = logging.getLogger("job_finder_api.ats_search")

PAGE_SIZE = 25
MATCH_LIMIT = 3000
SYNONYM_TTL = 30 * 24 * 3600
_WORD = re.compile(r"[a-z0-9+#]+")
_SENIORITY = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "intern", "trainee", "fresher",
              "associate", "i", "ii", "iii", "iv", "1", "2", "3", "head", "chief", "mid", "level", "entry"}
ABBREVIATIONS = {
    "sde": "software engineer", "swe": "software engineer", "sdet": "test engineer", "pm": "product manager",
    "ml": "machine learning", "qa": "qa engineer", "sre": "site reliability engineer", "fe": "frontend",
    "be": "backend", "fullstack": "full stack", "devops": "devops", "hr": "hr", "ux": "ux", "ui": "ui",
    "js": "javascript", "dev": "developer", "eng": "engineer", "mgr": "manager", "ba": "business analyst",
}
SYNONYM_GROUPS: list[tuple[str, ...]] = [
    ("software engineer", "software developer", "programmer", "application developer"),
    ("backend engineer", "backend developer", "back end engineer", "server engineer"),
    ("frontend engineer", "frontend developer", "front end developer", "ui engineer", "web developer"),
    ("full stack engineer", "full stack developer"),
    ("machine learning engineer", "ai engineer", "ml engineer", "applied scientist", "deep learning engineer"),
    ("data scientist", "machine learning scientist", "research scientist"),
    ("data engineer", "big data engineer", "etl developer", "analytics engineer"),
    ("data analyst", "analytics analyst", "bi analyst", "business intelligence analyst"),
    ("devops engineer", "site reliability engineer", "platform engineer", "infrastructure engineer"),
    ("cloud engineer", "cloud architect", "solutions architect"),
    ("product manager", "product owner"),
    ("project manager", "program manager", "delivery manager"),
    ("qa engineer", "test engineer", "quality assurance engineer", "automation tester", "sdet"),
    ("mobile developer", "android developer", "ios developer", "mobile engineer"),
    ("product designer", "ux designer", "ui designer", "ui ux designer"),
    ("security engineer", "cybersecurity engineer", "application security engineer", "security analyst"),
    ("embedded engineer", "firmware engineer", "embedded software engineer"),
    ("account executive", "sales executive", "business development executive"),
    ("customer success manager", "account manager", "client success manager"),
    ("hr business partner", "people partner", "hr generalist"),
    ("technical writer", "documentation engineer"),
    ("python developer", "python engineer"),
    ("java developer", "java engineer"),
    ("react developer", "react engineer"),
    ("business analyst", "product analyst"),
    ("support engineer", "technical support engineer", "customer support engineer"),
]
_SYNONYMS = {phrase: group for group in SYNONYM_GROUPS for phrase in group}

_CITY_GROUPS = (("bangalore", "bengaluru"), ("gurgaon", "gurugram"), ("mumbai", "bombay"), ("chennai", "madras"),
                ("kolkata", "calcutta"), ("delhi", "new delhi", "ncr"), ("pune", "poona"))
_CITY_ALIASES = {c: g for g in _CITY_GROUPS for c in g}

EXPAND_PROMPT = """You list other job titles employers use for the same role.
Reply with a JSON array of at most 5 short job titles (2-4 words each), no seniority words, no explanations.
Only titles that mean the same job, not adjacent or more senior roles."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _words(text_: str) -> list[str]:
    return _WORD.findall((text_ or "").lower())


def normalize_query(q: str) -> str:
    words = []
    for w in _words(q):
        if w in _SENIORITY:
            continue
        words.extend(ABBREVIATIONS.get(w, w).split())
    return " ".join(words)


def _cache_key(q: str) -> str:
    return f"ats:syn:{hashlib.sha256(q.encode()).hexdigest()[:24]}"


def expansions(q: str) -> tuple[list[str], bool]:
    """(phrases to match, known?). known=False -> nothing in the table or the AI cache yet."""
    norm = normalize_query(q)
    if not norm:
        return [], True
    found = list(_SYNONYMS.get(norm, ()))
    cached = get_redis().get(_cache_key(norm))
    if cached:
        try:
            found += [normalize_query(t) for t in json.loads(cached)]
        except (ValueError, TypeError):
            pass
    out: list[str] = []
    for phrase in [norm, *found]:
        if phrase and phrase not in out:
            out.append(phrase)
    return out[:8], bool(found) or cached is not None


def expand_with_ai(db: Session, q: str, user_id: str | None) -> list[str]:
    """Background: asks the router for other titles of the same role and caches them (empty list on failure)."""
    from app.services import ai_router

    norm = normalize_query(q)
    titles: list[str] = []
    try:
        result = ai_router.complete(db, user_id=user_id, task="title_suggest", system=EXPAND_PROMPT,
                                    user_prompt=f"Job title: {norm}", json_mode=True, max_tokens=300)
        data = json.loads(result.text)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        titles = [t.strip() for t in data if isinstance(t, str) and 2 <= len(t.strip()) <= 60
                  and re.fullmatch(r"[A-Za-z0-9 +#/&.-]+", t.strip())][:5]
    except Exception as e:  # noqa: BLE001 — no model / bad reply: the table alone is used
        logger.info("ats title expansion failed for %r: %s", norm, type(e).__name__)
    get_redis().set(_cache_key(norm), json.dumps(titles), ex=SYNONYM_TTL)
    return titles


def _pg(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _tsquery(phrases: list[str]) -> str:
    groups = []
    for p in phrases:
        terms = [f"{w}:A" for w in _words(p) if w.isalnum()]
        if terms:
            groups.append("(" + " & ".join(terms) + ")")
    return " | ".join(groups)


def _match_query(db: Session, phrases: list[str]):
    """Open jobs whose title matches any phrase, with a rank column, newest first within a rank."""
    base = db.query(AtsJob, AtsCompany).join(AtsCompany, AtsCompany.id == AtsJob.company_id).filter(
        AtsJob.closed_at.is_(None), AtsCompany.active.is_(True))
    if _pg(db):
        tsq = _tsquery(phrases)
        if not tsq:
            return None
        query = func.to_tsquery("english", tsq)
        rank = func.ts_rank(text("ats_jobs.tsv"), query).label("rank")
        return base.add_columns(rank).filter(text("ats_jobs.tsv @@ to_tsquery('english', :tsq)")).params(
            tsq=tsq).order_by(rank.desc(), func.coalesce(AtsJob.posted_at, AtsJob.first_seen_at).desc())
    conds = []
    for p in phrases:
        words = _words(p)
        if words:
            conds.append(and_(*[AtsJob.title_norm.like(f"%{w}%") for w in words]))
    if not conds:
        return None
    return base.add_columns(func.coalesce(AtsJob.posted_at, AtsJob.first_seen_at).label("rank")).filter(
        or_(*conds)).order_by(func.coalesce(AtsJob.posted_at, AtsJob.first_seen_at).desc())


def split_locations(location: str | list[str] | None) -> list[str]:
    """"Mumbai, India, Remote" or ["Mumbai", "India"] -> each place once, lower case."""
    raw = [location] if isinstance(location, str) else list(location or [])
    out: list[str] = []
    for value in raw:
        for part in (value or "").split(","):
            part = part.strip().lower()
            if part and part not in out:
                out.append(part)
    return out


def _filtered(db: Session, query, *, platforms_: list[str] | None, location: str | list[str] | None,
              remote: bool | None, posted_within_days: int | None, enabled: set[str]):
    allowed = [p for p in (platforms_ or enabled) if p in enabled]
    query = query.filter(AtsCompany.platform.in_(allowed or ["-"]))
    if remote is True:
        query = query.filter(AtsJob.remote.is_(True))
    elif remote is False:
        query = query.filter(AtsJob.remote.is_(False))
    places = split_locations(location)
    if places:
        # Any of the places (QA2 §4: the user's locations from Profile by default); "Remote" also
        # matches jobs the platform marks remote.
        conds = []
        for place in places:
            like = f"%{place}%"
            conds += [func.lower(AtsJob.location).like(like), func.lower(AtsJob.country).like(like)]
            if place == "remote":
                conds.append(AtsJob.remote.is_(True))
        query = query.filter(or_(*conds))
    if posted_within_days:
        cutoff = _now() - timedelta(days=posted_within_days)
        query = query.filter(func.coalesce(AtsJob.posted_at, AtsJob.first_seen_at) >= cutoff)
    return query


def job_out(job: AtsJob, applied: bool = False) -> dict:
    return {"id": job.id, "title": job.title, "location": job.location, "remote": job.remote,
            "department": job.department, "url": job.url, "posted_at": job.posted_at,
            "first_seen_at": job.first_seen_at, "applied": applied, "more_locations": []}


def registry_stats(db: Session) -> dict:
    r = get_redis()
    cached = r.get("ats:stats")
    if cached:
        data = json.loads(cached)
        if data.get("last_refresh_at"):
            data["last_refresh_at"] = datetime.fromisoformat(data["last_refresh_at"])
        return data
    companies = db.query(func.count(AtsCompany.id)).filter(AtsCompany.active.is_(True)).scalar() or 0
    jobs = ats_crawler.index_size(db)
    last = db.query(func.max(AtsCompany.last_ok_at)).scalar()
    by_platform = dict(db.query(AtsCompany.platform, func.count(AtsCompany.id)).filter(
        AtsCompany.active.is_(True)).group_by(AtsCompany.platform).all())
    data = {"companies": companies, "jobs": jobs, "last_refresh_at": last, "by_platform": by_platform}
    r.set("ats:stats", json.dumps(data, default=lambda v: v.isoformat()), ex=60)
    return data


def search(db: Session, user_id: str, q: str, *, platforms_: list[str] | None = None,
           location: str | list[str] | None = None, remote: bool | None = None,
           posted_within_days: int | None = None, any_age: bool = False, hide_applied: bool = False,
           page: int = 1) -> dict:
    """`any_age` keeps postings older than ATS_MAX_POSTING_AGE_DAYS, which are left out by default."""
    cfg = get_settings()
    phrases, known = expansions(q)
    if not posted_within_days and not any_age:
        posted_within_days = cfg.ATS_MAX_POSTING_AGE_DAYS or None
    location = split_locations(location)
    filters = {"p": sorted(platforms_ or []), "l": location, "r": remote,
               "d": posted_within_days, "h": hide_applied, "page": page}
    key = "ats:search:" + hashlib.sha256(json.dumps(
        [phrases, filters, user_id if hide_applied else ""], default=str).encode()).hexdigest()[:32]
    r = get_redis()
    cached = r.get(key)
    if cached:
        result = json.loads(cached)
    else:
        result = _search(db, user_id, phrases, filters=filters, platforms_=platforms_, location=location,
                         remote=remote, posted_within_days=posted_within_days, hide_applied=hide_applied, page=page)
        r.set(key, json.dumps(result, default=lambda v: v.isoformat()), ex=cfg.ATS_SEARCH_CACHE_SECONDS)
    _mark_applied(db, user_id, result["groups"])
    result["expansions"] = phrases
    result["expansion_pending"] = not known
    result["registry"] = registry_stats(db)
    _mark_hot(db, [g["company_id"] for g in result["groups"]])
    return result


def _search(db: Session, user_id: str, phrases: list[str], *, filters: dict, platforms_, location, remote,
            posted_within_days, hide_applied, page) -> dict:
    empty = {"groups": [], "total_companies": 0, "total_jobs": 0, "page": page, "page_size": PAGE_SIZE}
    query = _match_query(db, phrases)
    if query is None:
        return empty
    query = _filtered(db, query, platforms_=platforms_, location=location, remote=remote,
                      posted_within_days=posted_within_days, enabled=ats_registry.enabled_platforms(db))
    rows = query.limit(MATCH_LIMIT).all()
    applied_keys = set()
    if rows and hide_applied:
        keys = list({job.job_key for job, _, _ in rows})
        applied_keys = {k for (k,) in db.query(Application.job_key).filter(
            Application.user_id == user_id, Application.job_key.in_(keys)).all()} if keys else set()
    groups: dict[str, dict] = {}
    order: list[str] = []
    total_jobs = 0
    for job, company, _ in rows:
        applied = job.job_key in applied_keys
        if hide_applied and applied:
            continue
        total_jobs += 1
        g = groups.get(company.id)
        if g is None:
            impl = platforms.get(company.platform)
            g = groups[company.id] = {
                "company_id": company.id, "name": company.name, "platform": company.platform,
                "platform_name": impl.name if impl else company.platform, "careers_url": company.careers_url,
                "open_jobs": company.job_count, "matching_jobs": 0, "top_jobs": [], "locations": [],
                "newest_posted_at": None, "last_refreshed_at": company.last_ok_at}
            order.append(company.id)
        g["matching_jobs"] += 1
        # QA2 §4: one row per role — the same title posted for other places adds a location to it.
        same = next((t for t in g["top_jobs"] if t["title"].strip().lower() == job.title.strip().lower()), None)
        if same is not None:
            place = job.location or ("Remote" if job.remote else "")
            if place and place != same["location"] and place not in same["more_locations"]:
                same["more_locations"].append(place)
        elif len(g["top_jobs"]) < 3:
            g["top_jobs"].append(job_out(job))
        if job.location and job.location not in g["locations"] and len(g["locations"]) < 5:
            g["locations"].append(job.location)
        posted = job.posted_at or job.first_seen_at
        if posted and (g["newest_posted_at"] is None or posted > g["newest_posted_at"]):
            g["newest_posted_at"] = posted
    # Most matching jobs first; the newest posting breaks ties.
    order.sort(key=lambda c: (groups[c]["matching_jobs"], groups[c]["newest_posted_at"] or datetime.min),
               reverse=True)
    start = (page - 1) * PAGE_SIZE
    return {"groups": [groups[c] for c in order[start:start + PAGE_SIZE]], "total_companies": len(order),
            "total_jobs": total_jobs, "page": page, "page_size": PAGE_SIZE}


def _mark_applied(db: Session, user_id: str, groups: list[dict]) -> None:
    """Per-user "applied" flags, set after the (shared) cache so one user's applications never show for another."""
    jobs = [j for g in groups for j in g["top_jobs"]]
    if not jobs:
        return
    applied = {i for (i,) in db.query(AtsJob.id).join(Application, Application.job_key == AtsJob.job_key).filter(
        Application.user_id == user_id, AtsJob.id.in_([j["id"] for j in jobs])).all()}
    for j in jobs:
        j["applied"] = j["id"] in applied


def _mark_hot(db: Session, company_ids: list[str]) -> None:
    if not company_ids:
        return
    try:
        db.execute(update(AtsCompany).where(AtsCompany.id.in_(company_ids)).values(
            hot_until=_now() + timedelta(days=7)))
        db.commit()
    except Exception:  # noqa: BLE001 — refresh priority is a nicety; never fail a search for it
        db.rollback()


def company_jobs(db: Session, user_id: str, company: AtsCompany, q: str | None = None) -> list[dict]:
    query = db.query(AtsJob).filter(AtsJob.company_id == company.id, AtsJob.closed_at.is_(None))
    if q:
        for w in _words(normalize_query(q)):
            query = query.filter(AtsJob.title_norm.like(f"%{w}%"))
    jobs = query.order_by(func.coalesce(AtsJob.posted_at, AtsJob.first_seen_at).desc()).limit(500).all()
    keys = [j.job_key for j in jobs]
    applied = {k for (k,) in db.query(Application.job_key).filter(
        Application.user_id == user_id, Application.job_key.in_(keys)).all()} if keys else set()
    return [job_out(j, j.job_key in applied) for j in jobs]


# ---------------------------------------------------------------- full descriptions

def full_description(db: Session, job: AtsJob, company: AtsCompany | None = None) -> str:
    """Full text for one job, from the platform when it has a per-job call; cached a day. Falls back to the snippet."""
    r = get_redis()
    key = f"ats:desc:{job.id}"
    cached = r.get(key)
    if cached is not None:
        return cached
    company = company or db.get(AtsCompany, job.company_id)
    impl = platforms.get(company.platform) if company else None
    text_ = ""
    if impl is not None:
        ref = ats_registry.to_ref(company)
        try:
            if company.platform == "workday":
                text_ = impl.job_detail_by_url(ref, job.url)
            elif impl.has_detail:
                text_ = impl.job_detail(ref, job.external_id)
        except SourceError as e:
            logger.info("ats detail failed for %s: %s", job.id, e)
    text_ = (text_ or job.snippet or "")[:8000]
    r.set(key, text_, ex=24 * 3600)
    return text_


# ---------------------------------------------------------------- live top-up

def topup_lock(q: str) -> bool:
    """One top-up per normalised query per 10 minutes."""
    return bool(get_redis().set(f"ats:topup:{_cache_key(normalize_query(q))}", "1", nx=True, ex=600))


def topup(session_factory: Callable[[], Session], q: str, *, progress: Callable[[str], None] = lambda m: None) -> dict:
    """Live-searches / refreshes stale companies for a query with few index matches (P6 plan §3.4.5).
    Workday and SmartRecruiters tenants are searched by text; other companies are crawled in full."""
    cfg = get_settings()
    phrases, _ = expansions(q)
    db = session_factory()
    try:
        enabled = ats_registry.enabled_platforms(db)
        stale_before = _now() - timedelta(hours=cfg.ATS_HOT_REFRESH_HOURS)
        searchable = [c for (c,) in db.query(AtsCompany.id).filter(
            AtsCompany.active.is_(True), AtsCompany.platform.in_([p for p in ("workday", "smartrecruiters")
                                                                  if p in enabled])
        ).order_by(AtsCompany.job_count.desc()).limit(cfg.ATS_TOPUP_MAX_COMPANIES // 2).all()]
        stale = [c for (c,) in db.query(AtsCompany.id).filter(
            AtsCompany.active.is_(True), AtsCompany.platform.in_(list(enabled)),
            AtsCompany.platform.notin_(["workday", "smartrecruiters"]),
            or_(AtsCompany.last_crawled_at.is_(None), AtsCompany.last_crawled_at < stale_before),
        ).order_by(AtsCompany.last_crawled_at.is_(None).desc(), AtsCompany.last_crawled_at).limit(
            cfg.ATS_TOPUP_MAX_COMPANIES - len(searchable)).all()]
    finally:
        db.close()
    progress(f"checking {len(searchable) + len(stale)} company career pages live")
    deadline = time.monotonic() + cfg.ATS_TOPUP_SECONDS
    counts = {"searched": 0, "crawled": 0, "added": 0, "errors": 0}

    def search_one(company_id: str) -> None:
        s = session_factory()
        try:
            company = s.get(AtsCompany, company_id)
            impl = platforms.get(company.platform)
            found: dict[str, dict] = {}
            with ats_crawler.host_slot(ats_crawler.api_host(company)):
                for phrase in phrases[:3]:
                    if time.monotonic() > deadline:
                        break
                    for job in impl.search(ats_registry.to_ref(company), phrase, limit=50):
                        found.setdefault(job["external_id"], job)
            res = ats_crawler.upsert_jobs(s, company, list(found.values()), complete=False, close=False)
            s.commit()
            counts["searched"] += 1
            counts["added"] += res["added"]
        except Exception as e:  # noqa: BLE001
            s.rollback()
            counts["errors"] += 1
            logger.info("ats top-up search failed for %s: %s", company_id, type(e).__name__)
        finally:
            s.close()

    def crawl_one(company_id: str) -> None:
        s = session_factory()
        try:
            res = ats_crawler.crawl_company(s, company_id)
            counts["crawled"] += 1
            counts["added"] += res.get("added", 0)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            counts["errors"] += 1
            logger.info("ats top-up crawl failed for %s: %s", company_id, type(e).__name__)
        finally:
            s.close()

    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(search_one, c) for c in searchable] + [pool.submit(crawl_one, c) for c in stale]
    wait(futures, timeout=max(1.0, deadline - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    _clear_search_cache()
    progress(f"top-up done: {counts['added']} new jobs")
    return counts


def _clear_search_cache() -> None:
    r = get_redis()
    for key in r.scan_iter("ats:search:*", count=500):
        r.delete(key)
    r.delete("ats:stats")


# ---------------------------------------------------------------- pipeline source

def index_jobs(db: Session, keyword: str, *, locations: list[str], country: str, max_age_hours: int,
               limit: int, detail_budget: int = 10) -> list[dict]:
    """Index jobs for one pipeline keyword, in the normal job shape (source = the ATS platform name, so
    dedupe ranks them "company ATS" and the Apply Center sees Greenhouse / Lever links)."""
    phrases, _ = expansions(keyword)
    query = _match_query(db, phrases)
    if query is None:
        return []
    query = _filtered(db, query, platforms_=None, location=None, remote=None,
                      posted_within_days=max(1, -(-max_age_hours // 24)), enabled=ats_registry.enabled_platforms(db))
    places = [p.strip().lower() for p in locations if p.strip()]
    cities = {c for p in places if "remote" not in p and p != (country or "").lower()
              for c in _CITY_ALIASES.get(p.split(",")[0].strip(), (p.split(",")[0].strip(),))}

    def place_ok(job: AtsJob) -> bool:
        loc = f"{job.location} {job.country}".lower()
        if job.remote:
            return geo_allows(job.location, country)
        if cities:
            return any(c in loc for c in cities)
        return not country or country.lower() in loc

    out: list[tuple[AtsJob, AtsCompany]] = []
    for job, company, _ in query.limit(limit * 6).all():
        if place_ok(job):
            out.append((job, company))
            if len(out) >= limit:
                break
    descriptions: dict[str, str] = {}
    top = out[:detail_budget]
    if top:
        def fetch_one(pair: tuple[AtsJob, AtsCompany]) -> tuple[str, str]:
            with Session(bind=db.get_bind()) as local:
                return pair[0].id, full_description(local, pair[0], pair[1])

        with ThreadPoolExecutor(max_workers=4) as pool:
            descriptions = dict(pool.map(fetch_one, top))
    jobs = []
    for job, company in out:
        impl = platforms.get(company.platform)
        jobs.append({"source": impl.name if impl else company.platform, "title": job.title, "company": company.name,
                     "location": job.location or ("Remote" if job.remote else ""), "url": job.url,
                     "description": descriptions.get(job.id) or job.snippet,
                     "posted": (job.posted_at or job.first_seen_at).isoformat() + "+00:00", "apply_url": job.url})
    return jobs
