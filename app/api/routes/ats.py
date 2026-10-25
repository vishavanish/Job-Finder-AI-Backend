"""
app/api/routes/ats.py
------------------------
ATS Discovery (CLAUDE.md 7.6, P6):

  - GET  /ats/search?q=&platform=&location=&remote=&posted_within_days=&hide_applied=&page=
         companies hiring for a title, grouped, from the index; a live
         top-up task id when few match (poll GET /tasks/{id}, then search again)
  - GET  /ats/stats                         registry size, indexed jobs, last refresh
  - POST /ats/companies                     add a company by careers link (shared with everyone)
  - GET  /ats/companies/{id}/jobs?q=        one company's open jobs
  - GET  /ats/jobs/{id}                     one job with its full description
  - POST /ats/jobs/{id}/track               make it a normal job (save / hide / apply buttons work)

  Admin: GET/PATCH /admin/ats/companies[/{id}], POST /admin/ats/companies/{id}/crawl,
         GET /admin/ats/health, PATCH /admin/ats/platforms/{platform}

All entitlement-gated (`ats_discovery`); a search counts against its daily quota.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.ats import AtsCompany, AtsJob, AtsPlatformSetting
from app.models.jobs import Job
from app.models.ats_schemas import (
    AtsCompanyIn, AtsCompanyJobs, AtsCompanyOut, AtsCompanyUpdate, AtsHealth, AtsJobDetail, AtsPlatformOut,
    AtsPlatformUpdate, AtsRegistryStats, AtsSearchResult, AtsTracked,
)
from app.providers.ats import platforms
from app.services import ats_registry, ats_search, audit, entitlements, job_store, target_companies

router = APIRouter(tags=["ats"])
settings = get_settings()
admin_only = require_role("admin")


def _require_ats(db: Session, user) -> None:
    if not entitlements.effective(db, user.id, "ats_discovery").enabled:
        raise HTTPException(403, "Your account does not include company career pages. Ask an admin to enable it.")


def _company_out(c: AtsCompany, created: bool = False) -> AtsCompanyOut:
    impl = platforms.get(c.platform)
    return AtsCompanyOut(id=c.id, name=c.name, platform=c.platform, platform_name=impl.name if impl else c.platform,
                         slug=c.slug, careers_url=c.careers_url, origin=c.origin, active=c.active,
                         job_count=c.job_count, last_crawled_at=c.last_crawled_at, last_ok_at=c.last_ok_at,
                         next_crawl_at=c.next_crawl_at, last_error=c.last_error, created=created)


def _company(db: Session, company_id: str) -> AtsCompany:
    row = db.get(AtsCompany, company_id)
    if row is None:
        raise HTTPException(404, "No such company in the registry.")
    return row


@router.get("/ats/search", response_model=AtsSearchResult)
@limiter.limit(settings.RATE_LIMIT_ATS_SEARCH)
def search(request: Request, q: str = Query(..., min_length=2, max_length=120),
           platform: list[str] | None = Query(default=None),
           location: list[str] | None = Query(default=None, description="One or more places (or comma-separated); "
                                                                         "a job in any of them matches"),
           remote: bool | None = None, posted_within_days: int | None = Query(default=None, ge=1, le=365),
           any_age: bool = Query(False, description="Include postings older than ATS_MAX_POSTING_AGE_DAYS"),
           hide_applied: bool = False, page: int = Query(default=1, ge=1, le=100),
           user=Depends(entitlements.require_feature("ats_discovery")), db: Session = Depends(get_db)):
    from app.core.celery_tasks import ats_expand_title_task, ats_topup_task

    unknown = [p for p in platform or [] if platforms.get(p) is None]
    if unknown:
        raise HTTPException(422, f"Unknown platform: {', '.join(unknown)}")
    result = ats_search.search(db, user.id, q, platforms_=[p.lower() for p in platform or []] or None,
                               location=[loc[:80] for loc in location or []], remote=remote,
                               posted_within_days=posted_within_days, any_age=any_age,
                               hide_applied=hide_applied, page=page)
    if result["expansion_pending"] and ats_search.topup_lock(f"expand:{q}"):
        ats_expand_title_task.apply_async(args=[q, user.id], queue=settings.default_queue)
    followed = target_companies.company_followed(db, user.id, [g["company_id"] for g in result["groups"]])
    for g in result["groups"]:
        g["followed"] = g["company_id"] in followed
    if page == 1 and result["total_companies"] < settings.ATS_MIN_COMPANIES and ats_search.topup_lock(q):
        result["topup_task_id"] = str(dispatch(ats_topup_task, args=[q, user.id], queue=settings.default_queue,
                                               user_id=user.id).task_id)
    return {"query": q, **result}


@router.get("/ats/stats", response_model=AtsRegistryStats)
def stats(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    return ats_search.registry_stats(db)


@router.post("/ats/companies", response_model=AtsCompanyOut, status_code=201)
@limiter.limit(settings.RATE_LIMIT_ATS_ADD_COMPANY)
def add_company(request: Request, req: AtsCompanyIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Adds a company to the shared registry; its jobs appear after the first crawl (a minute or two)."""
    from app.core.celery_tasks import crawl_ats_company_task

    _require_ats(db, user)
    ref = ats_registry.parse_user_url(req.url) if req.url else ats_registry.ref_from_parts(req.platform, req.slug)
    row, created = ats_registry.get_or_create(db, ref, origin="user", name=req.name, user_id=user.id)
    if created:
        crawl_ats_company_task.apply_async(args=[row.id], queue=settings.maintenance_queue)
    return _company_out(row, created)


@router.get("/ats/companies/{company_id}/jobs", response_model=AtsCompanyJobs)
def company_jobs(company_id: str, q: str | None = Query(default=None, max_length=120),
                 user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    row = _company(db, company_id)
    return {"company": _company_out(row), "jobs": ats_search.company_jobs(db, user.id, row, q),
            "followed": bool(target_companies.company_followed(db, user.id, [row.id]))}


def _ats_job(db: Session, job_id: str) -> tuple[AtsJob, AtsCompany]:
    job = db.get(AtsJob, job_id)
    if job is None or job.closed_at is not None:
        raise HTTPException(404, "This job is no longer open.")
    return job, db.get(AtsCompany, job.company_id)


def _as_pipeline_job(job: AtsJob, company: AtsCompany, description: str) -> dict:
    impl = platforms.get(company.platform)
    return {"source": impl.name if impl else company.platform, "title": job.title, "company": company.name,
            "location": job.location, "url": job.url, "description": description, "apply_url": job.url,
            "posted": (job.posted_at or job.first_seen_at).isoformat() + "+00:00"}


@router.get("/ats/jobs/{job_id}", response_model=AtsJobDetail)
def job_detail(job_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    job, company = _ats_job(db, job_id)
    description = ats_search.full_description(db, job, company)
    tracked = db.query(Job.id).filter(Job.job_key == job.job_key).first()
    out = ats_search.job_out(job)
    return {**out, "company_id": company.id, "company": company.name, "platform": company.platform,
            "description": description, "job_id": tracked[0] if tracked else None}


@router.post("/ats/jobs/{job_id}/track", response_model=AtsTracked)
def track_job(job_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Stores the job in the canonical jobs table (same dedupe as search runs) and returns its id, so the
    usual /jobs/{id}/save|hide, /apply/options/{id} and /applications/manual work on it."""
    _require_ats(db, user)
    job, company = _ats_job(db, job_id)
    row = job_store.upsert_copy(db, _as_pipeline_job(job, company, ats_search.full_description(db, job, company)),
                                job.job_key)
    return {"job_id": row.id}


# ---------------------------------------------------------------- admin

def _platform_rows(db: Session) -> list[AtsPlatformOut]:
    from sqlalchemy import func
    settings_rows = ats_registry.platform_settings(db)
    counts = dict(db.query(AtsCompany.platform, func.count(AtsCompany.id)).filter(
        AtsCompany.active.is_(True)).group_by(AtsCompany.platform).all())
    out = []
    for key, impl in platforms.PLATFORMS.items():
        row = settings_rows.get(key)
        out.append(AtsPlatformOut(platform=key, name=impl.name, enabled=row.enabled if row else True,
                                  companies=counts.get(key, 0),
                                  consecutive_failures=row.consecutive_failures if row else 0,
                                  last_success_at=row.last_success_at if row else None,
                                  last_error_at=row.last_error_at if row else None,
                                  last_error=row.last_error if row else None))
    return out


@router.get("/admin/ats/health", response_model=AtsHealth)
def admin_health(actor=Depends(admin_only), db: Session = Depends(get_db)):
    failing = db.query(AtsCompany).filter(AtsCompany.active.is_(True), AtsCompany.fail_count > 0).order_by(
        AtsCompany.fail_count.desc()).limit(50).all()
    off = db.query(AtsCompany.id).filter(AtsCompany.active.is_(False)).count()
    return {"registry": ats_search.registry_stats(db), "index_limit": settings.ATS_MAX_INDEXED_JOBS,
            "platforms": _platform_rows(db), "failing_companies": [_company_out(c) for c in failing],
            "switched_off": off}


@router.get("/admin/ats/companies", response_model=list[AtsCompanyOut])
def admin_companies(q: str | None = Query(default=None, max_length=100), platform: str | None = None,
                    active: bool | None = None, limit: int = Query(default=100, ge=1, le=500),
                    offset: int = Query(default=0, ge=0), actor=Depends(admin_only), db: Session = Depends(get_db)):
    query = db.query(AtsCompany)
    if q:
        query = query.filter(AtsCompany.name.ilike(f"%{q}%") | AtsCompany.slug.ilike(f"%{q}%"))
    if platform:
        query = query.filter(AtsCompany.platform == platform)
    if active is not None:
        query = query.filter(AtsCompany.active.is_(active))
    return [_company_out(c) for c in query.order_by(AtsCompany.name).offset(offset).limit(limit).all()]


@router.post("/admin/ats/companies", response_model=AtsCompanyOut, status_code=201)
def admin_add_company(req: AtsCompanyIn, request: Request, actor=Depends(admin_only), db: Session = Depends(get_db)):
    from app.core.celery_tasks import crawl_ats_company_task

    ref = ats_registry.parse_user_url(req.url) if req.url else ats_registry.ref_from_parts(req.platform, req.slug)
    row, created = ats_registry.get_or_create(db, ref, origin="admin", name=req.name, user_id=actor.id)
    if created:
        audit.record(db, actor_id=actor.id, action="ats_company.create", target_type="ats_company",
                     target_id=row.id, after={"ref_key": row.ref_key}, request=request)
        db.commit()
        crawl_ats_company_task.apply_async(args=[row.id], queue=settings.maintenance_queue)
    return _company_out(row, created)


@router.patch("/admin/ats/companies/{company_id}", response_model=AtsCompanyOut)
def admin_edit_company(company_id: str, req: AtsCompanyUpdate, request: Request, actor=Depends(admin_only),
                       db: Session = Depends(get_db)):
    row = _company(db, company_id)
    before = {"name": row.name, "active": row.active}
    if req.name is not None:
        row.name, row.origin = req.name.strip()[:255] or row.name, "admin"
    if req.active is not None:
        row.active = req.active
        if req.active:
            row.gone_count = row.fail_count = 0
            row.next_crawl_at = ats_registry.now()
    audit.record(db, actor_id=actor.id, action="ats_company.update", target_type="ats_company", target_id=row.id,
                 before=before, after={"name": row.name, "active": row.active}, request=request)
    db.commit()
    return _company_out(row)


@router.post("/admin/ats/companies/{company_id}/crawl", response_model=AtsCompanyOut)
def admin_crawl_now(company_id: str, actor=Depends(admin_only), db: Session = Depends(get_db)):
    from app.core.celery_tasks import crawl_ats_company_task

    row = _company(db, company_id)
    crawl_ats_company_task.apply_async(args=[row.id], queue=settings.maintenance_queue)
    return _company_out(row)


@router.patch("/admin/ats/platforms/{platform}", response_model=AtsPlatformOut)
def admin_edit_platform(platform: str, req: AtsPlatformUpdate, request: Request, actor=Depends(admin_only),
                        db: Session = Depends(get_db)):
    if platforms.get(platform) is None:
        raise HTTPException(404, "Unknown platform.")
    row = db.get(AtsPlatformSetting, platform)
    if row is None:
        row = AtsPlatformSetting(platform=platform, enabled=True, consecutive_failures=0)
        db.add(row)
    before = {"enabled": row.enabled}
    row.enabled = req.enabled
    audit.record(db, actor_id=actor.id, action="ats_platform.update", target_type="ats_platform",
                 target_id=platform, before=before, after={"enabled": req.enabled}, request=request)
    db.commit()
    return next(p for p in _platform_rows(db) if p.platform == platform)
