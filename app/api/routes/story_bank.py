"""
app/api/routes/story_bank.py
------------------------------
The user's STAR stories (CLAUDE.md 7.10, P7):

  - GET    /story-bank                      stories (drafts waiting for review first)
  - POST   /story-bank                      add one
  - PUT    /story-bank/{id}                 edit (editing a draft approves it)
  - DELETE /story-bank/{id}
  - POST   /story-bank/{id}/approve         use an AI draft
  - POST   /story-bank/draft-from-resume    202 + task_id: AI drafts from the master resume (unapproved)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.voice_schemas import StoryIn, StoryList, StoryOut, TaskQueuedOut
from app.services import resume_store, story_bank
from app.services.entitlements import require_feature
from app.services.voice_store import api_error

router = APIRouter(prefix="/story-bank", tags=["voice"])


@router.get("", response_model=StoryList)
def list_stories(user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows = story_bank.list_stories(db, user.id)
    return {"stories": [story_bank.story_out(s) for s in rows], "total": len(rows),
            "needs_review": sum(1 for s in rows if not s.approved)}


@router.post("", response_model=StoryOut, status_code=201)
def create_story(body: StoryIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return story_bank.story_out(story_bank.create(db, user.id, body.model_dump()))


@router.post("/draft-from-resume", response_model=TaskQueuedOut, status_code=202)
@limiter.limit(lambda: get_settings().RATE_LIMIT_PROFILE_AI)
def draft_from_resume(request: Request, user=Depends(require_feature("voice_prep")), db: Session = Depends(get_db)):
    master = resume_store.master(db, user.id)
    if master is None or not (master.text or "").strip():
        raise api_error(409, "resume_required", "Upload a resume (and mark it as master) first.")
    from app.core.celery_tasks import story_bank_draft_task
    task = dispatch(story_bank_draft_task, args=[user.id], queue=get_settings().default_queue, user_id=user.id)
    return {"task_id": task.task_id,
            "message": "Drafting stories from your resume — they'll appear here for you to review."}


@router.put("/{story_id}", response_model=StoryOut)
def update_story(story_id: str, body: StoryIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = story_bank.get_owned(db, user.id, story_id)
    return story_bank.story_out(story_bank.update(db, row, body.model_dump()))


@router.post("/{story_id}/approve", response_model=StoryOut)
def approve_story(story_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return story_bank.story_out(story_bank.approve(db, story_bank.get_owned(db, user.id, story_id)))


@router.delete("/{story_id}", status_code=204)
def delete_story(story_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    story_bank.delete(db, story_bank.get_owned(db, user.id, story_id))
