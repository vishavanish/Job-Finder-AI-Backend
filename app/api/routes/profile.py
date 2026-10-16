"""
app/api/routes/profile.py
---------------------------
Profile & Search (CLAUDE.md 7.2, 7.3):

  - GET/PUT /profile                      headline, summary, applicant info, work authorization
  - GET/PUT /preferences                  what jobs the user is looking for
  - POST /resumes                         upload PDF/DOCX (multipart `file`); parsed by a worker
  - GET /resumes, GET /resumes/{id}       versions / one version with text + parsed profile
  - GET /resumes/{id}/file                the original file
  - PUT /resumes/{id}                     save the reviewed parse result and/or label
  - POST /resumes/{id}/master             make this version the master
  - POST /resumes/{id}/parse              read it again with AI
  - DELETE /resumes/{id}
  - /search-profiles (CRUD), POST /search-profiles/from-resume,
    POST /search-profiles/{id}/suggest-titles
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.profile import JobPreference, Profile
from app.models.profile_schemas import (
    EeoAnswers, PreferencesIn, PreferencesOut, ProfileIn, ProfileOut, ResumeDetail, ResumeList, ResumeOut, ResumeUpdate,
    SearchProfileIn, SearchProfileList, SearchProfileOut, SeedFromResumeIn, TitleSuggestions,
)
from app.services import key_store, resume_store, search_profiles

router = APIRouter(tags=["profile"])
settings = get_settings()


# ---------------------------------------------------------------- profile

def _eeo(row: Profile | None) -> EeoAnswers:
    if row is None or not row.eeo_encrypted:
        return EeoAnswers()
    return EeoAnswers.model_validate_json(key_store.unseal(row.eeo_encrypted))


@router.get("/profile", response_model=ProfileOut)
def get_profile(user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(Profile).filter(Profile.user_id == user.id).first()
    if row is None:
        return ProfileOut(applicant_info={"email": user.email}, eeo=EeoAnswers())
    return ProfileOut(headline=row.headline, summary=row.summary, applicant_info=row.applicant_info or {},
                      work_authorization=row.work_authorization or [], eeo=_eeo(row), updated_at=row.updated_at)


@router.put("/profile", response_model=ProfileOut)
def put_profile(req: ProfileIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(Profile).filter(Profile.user_id == user.id).first()
    if row is None:
        row = Profile(user_id=user.id)
        db.add(row)
    row.headline, row.summary = req.headline, req.summary
    row.applicant_info = req.applicant_info.model_dump()
    row.work_authorization = [qa.model_dump() for qa in req.work_authorization]
    if req.eeo is not None:  # sensitive: encrypted at rest, never logged or sent to AI models
        row.eeo_encrypted = key_store.seal(req.eeo.model_dump_json())
    db.commit()
    return ProfileOut(**req.model_dump(exclude={"eeo"}), eeo=_eeo(row), updated_at=row.updated_at)


# ---------------------------------------------------------------- preferences

_PREF_FIELDS = tuple(PreferencesIn.model_fields)


@router.get("/preferences", response_model=PreferencesOut)
def get_preferences(user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(JobPreference).filter(JobPreference.user_id == user.id).first()
    if row is None:
        return PreferencesOut()
    return PreferencesOut(**{f: getattr(row, f) for f in _PREF_FIELDS}, updated_at=row.updated_at)


@router.put("/preferences", response_model=PreferencesOut)
def put_preferences(req: PreferencesIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(JobPreference).filter(JobPreference.user_id == user.id).first()
    if row is None:
        row = JobPreference(user_id=user.id)
        db.add(row)
    for f in _PREF_FIELDS:
        setattr(row, f, getattr(req, f))
    db.commit()
    return PreferencesOut(**req.model_dump(), updated_at=row.updated_at)


# ---------------------------------------------------------------- resumes

def _dispatch_parse(resume_id: str, user_id: str) -> None:
    from app.core.celery_tasks import parse_resume_task
    parse_resume_task.apply_async(args=[resume_id, user_id], queue=settings.default_queue)


@router.post("/resumes", response_model=ResumeDetail, status_code=201)
@limiter.limit(settings.RATE_LIMIT_RESUME_UPLOAD)
async def upload_resume(
    request: Request,
    file: UploadFile = File(..., description="PDF or DOCX"),
    label: str | None = Form(None, max_length=100),
    parse: bool = Form(True, description="Read it with AI right away"),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stores a new version (the first one becomes the master). With
    `parse` (default), a worker reads it with AI: poll GET /resumes/{id}
    until `parse_status` leaves "pending", then let the user review and
    save it with PUT /resumes/{id}."""
    data = await file.read(settings.RESUME_MAX_BYTES + 1)
    row = resume_store.create_resume(db, user_id=user.id, file_name=file.filename or "", data=data, label=label)
    if not parse and row.parse_status == "pending":
        row.parse_status = "unparsed"
    db.commit()
    if parse and row.parse_status == "pending":
        _dispatch_parse(row.id, user.id)
    return row


@router.get("/resumes", response_model=ResumeList)
def list_resumes(user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows = resume_store.list_resumes(db, user.id)
    return {"resumes": rows, "master_id": next((r.id for r in rows if r.is_master), None)}


@router.get("/resumes/{resume_id}", response_model=ResumeDetail)
def get_resume(resume_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return resume_store.get_owned(db, user.id, resume_id)


@router.get("/resumes/{resume_id}/file")
def download_resume(resume_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = resume_store.get_owned(db, user.id, resume_id)
    return Response(
        content=resume_store.load_file(db, row), media_type=row.content_type,
        headers={"Content-Disposition": f'attachment; filename="{row.file_name}"',
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@router.put("/resumes/{resume_id}", response_model=ResumeDetail)
def update_resume(resume_id: str, req: ResumeUpdate, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = resume_store.get_owned(db, user.id, resume_id)
    resume_store.save_review(db, row, label=req.label, parsed=req.parsed, fields_set=req.model_fields_set)
    db.commit()
    return row


@router.post("/resumes/{resume_id}/master", response_model=ResumeOut)
def make_master(resume_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = resume_store.set_master(db, user.id, resume_id)
    db.commit()
    return row


@router.post("/resumes/{resume_id}/parse", response_model=ResumeOut, status_code=202)
@limiter.limit(settings.RATE_LIMIT_PROFILE_AI)
def reparse_resume(request: Request, resume_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Reads the resume again with AI. Replaces the parsed profile when it
    finishes (including edits saved earlier)."""
    row = resume_store.get_owned(db, user.id, resume_id)
    if not row.text:
        raise resume_store.api_error(422, "resume_no_text", "This file has no text to read.")
    row.parse_status, row.parse_error = "pending", None
    db.commit()
    _dispatch_parse(row.id, user.id)
    return row


@router.delete("/resumes/{resume_id}", status_code=204)
def delete_resume(resume_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    resume_store.delete_resume(db, user.id, resume_id)
    db.commit()


# ---------------------------------------------------------------- search profiles

@router.get("/search-profiles", response_model=SearchProfileList)
def list_search_profiles(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return {"profiles": [search_profiles.to_out(r) for r in search_profiles.list_profiles(db, user.id)]}


@router.post("/search-profiles", response_model=SearchProfileOut, status_code=201)
def create_search_profile(req: SearchProfileIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = search_profiles.create_profile(db, user.id, req)
    db.commit()
    return search_profiles.to_out(row)


@router.post("/search-profiles/from-resume", response_model=SearchProfileOut, status_code=201)
def seed_search_profile(req: SeedFromResumeIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """A new profile with the skills and recent titles from a parsed resume
    (the master by default). Weights start as "strong"; edit them after."""
    if req.resume_id:
        resume = resume_store.get_owned(db, user.id, req.resume_id)
    else:
        resume = resume_store.master(db, user.id)
        if resume is None:
            raise resume_store.api_error(422, "resume_required", "Upload a resume first.")
    row = search_profiles.seed_from_resume(db, user.id, name=req.name, resume=resume)
    db.commit()
    return search_profiles.to_out(row)


@router.get("/search-profiles/{profile_id}", response_model=SearchProfileOut)
def get_search_profile(profile_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return search_profiles.to_out(search_profiles.get_owned(db, user.id, profile_id))


@router.put("/search-profiles/{profile_id}", response_model=SearchProfileOut)
def update_search_profile(profile_id: str, req: SearchProfileIn, user=Depends(get_current_user),
                          db: Session = Depends(get_db)):
    row = search_profiles.update_profile(db, search_profiles.get_owned(db, user.id, profile_id), req)
    db.commit()
    return search_profiles.to_out(row)


@router.delete("/search-profiles/{profile_id}", status_code=204)
def delete_search_profile(profile_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    search_profiles.delete_profile(db, search_profiles.get_owned(db, user.id, profile_id))
    db.commit()


@router.post("/search-profiles/{profile_id}/suggest-titles", response_model=TitleSuggestions)
@limiter.limit(settings.RATE_LIMIT_PROFILE_AI)
def suggest_titles(request: Request, profile_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Adjacent job titles from the profile's skills (AI). Nothing is saved:
    the user picks which to add."""
    row = search_profiles.get_owned(db, user.id, profile_id)
    titles, engine = search_profiles.suggest_titles(db, user.id, row)
    db.commit()  # usage ledger rows
    return {"titles": titles, "engine": engine}
