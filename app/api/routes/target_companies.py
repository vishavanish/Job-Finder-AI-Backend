"""
app/api/routes/target_companies.py
-------------------------------------
Target companies (QA4): follow companies and get told first when they post
a matching role.

  - GET    /ats/following                      followed companies + alert settings
  - PUT    /ats/companies/{id}/follow          follow a registry company
  - DELETE /ats/companies/{id}/follow          unfollow it
  - DELETE /ats/following/{follow_id}          remove any follow (also one still being looked for)
  - POST   /ats/following/by-name {name}       follow by name; unknown names are looked up in the background
  - GET    /ats/following/new?company_id=      new matching roles at followed companies
  - POST   /ats/following/seen {company_id?}   mark them seen
  - GET    /ats/suggestions                    companies hiring for the user's titles in their places
  - GET/PUT /ats/following/settings            alert mode, titles, places, time zone

All need the `ats_discovery` entitlement.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.ats import AtsCompany
from app.models.ats_schemas import (
    FollowByNameIn, FollowByNameOut, FollowedCompany, FollowingOut, FollowSettingsIn, FollowSettingsOut,
    NewRolesOut, SeenIn, SuggestionsOut,
)
from app.services import entitlements, target_companies

router = APIRouter(tags=["ats"])
settings = get_settings()


def _require_ats(db: Session, user) -> None:
    if not entitlements.effective(db, user.id, "ats_discovery").enabled:
        raise HTTPException(403, "Your account does not include company career pages. Ask an admin to enable it.")


def _one(db: Session, user_id: str, follow_id: str) -> FollowedCompany:
    return next(FollowedCompany(**c) for c in target_companies.following(db, user_id) if c["id"] == follow_id)


@router.get("/ats/following", response_model=FollowingOut)
def following(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    row = target_companies.get_settings_row(db, user.id)
    return {"companies": target_companies.following(db, user.id), "limit": settings.TARGET_COMPANIES_MAX_PER_USER,
            "settings": target_companies.settings_out(db, row)}


@router.put("/ats/companies/{company_id}/follow", response_model=FollowedCompany)
@limiter.limit(settings.RATE_LIMIT_ATS_FOLLOW)
def follow(request: Request, company_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    company = db.get(AtsCompany, company_id)
    if company is None or not company.active:
        raise HTTPException(404, "No such company in the registry.")
    row = target_companies.follow(db, user.id, company)
    return _one(db, user.id, row.id)


@router.delete("/ats/companies/{company_id}/follow", status_code=204)
def unfollow(company_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    target_companies.unfollow(db, user.id, company_id=company_id)
    return Response(status_code=204)


@router.delete("/ats/following/{follow_id}", status_code=204)
def remove_follow(follow_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    target_companies.unfollow(db, user.id, follow_id=follow_id)
    return Response(status_code=204)


@router.post("/ats/following/by-name", response_model=FollowByNameOut)
@limiter.limit(settings.RATE_LIMIT_ATS_FOLLOW)
def follow_by_name(request: Request, req: FollowByNameIn, user=Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Follows a company by name. A name we already know is followed at once; otherwise its career page
    is looked up in the background (status "searching", then "following" or "not_found")."""
    from app.core.celery_tasks import ats_follow_lookup_task

    _require_ats(db, user)
    row, lookup = target_companies.follow_by_name(db, user.id, req.name)
    task_id = None
    if lookup:
        task_id = str(dispatch(ats_follow_lookup_task, args=[row.id], queue=settings.maintenance_queue,
                               user_id=user.id).task_id)
    return {"status": row.status, "follow": _one(db, user.id, row.id), "task_id": task_id}


@router.get("/ats/following/new", response_model=NewRolesOut)
def new_roles(company_id: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=100),
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    return {"roles": target_companies.new_roles(db, user.id, company_id=company_id, limit=limit)}


@router.post("/ats/following/seen")
def seen(req: SeenIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    return {"marked": target_companies.mark_seen(db, user.id, req.company_id)}


@router.get("/ats/suggestions", response_model=SuggestionsOut)
def suggestions(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    return {"companies": target_companies.suggestions(db, user.id)}


@router.get("/ats/following/settings", response_model=FollowSettingsOut)
def get_follow_settings(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    return target_companies.settings_out(db, target_companies.get_settings_row(db, user.id))


@router.put("/ats/following/settings", response_model=FollowSettingsOut)
def put_follow_settings(req: FollowSettingsIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_ats(db, user)
    row = target_companies.save_settings(db, user.id, alert_mode=req.alert_mode, titles=req.titles,
                                         places=req.places, tz=req.timezone)
    return target_companies.settings_out(db, row)
