"""
app/api/routes/answer_bank.py
--------------------------------
The user's saved screening answers (CLAUDE.md 7.2 "Answer bank", P4a).

  - GET    /answer-bank?approved=      list (AI drafts waiting for approval: approved=false)
  - POST   /answer-bank                save your own answer (approved)
  - PUT    /answer-bank/{id}           edit the answer (editing approves it) or set `approved`
  - POST   /answer-bank/{id}/approve   approve an AI draft as it is
  - DELETE /answer-bank/{id}

Only approved answers are ever submitted without you seeing them.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models.apply import AnswerBankEntry
from app.models.apply_schemas import AnswerIn, AnswerList, AnswerOut, AnswerUpdate
from app.services import answer_bank

router = APIRouter(prefix="/answer-bank", tags=["apply"])


def _out(row: AnswerBankEntry) -> AnswerOut:
    return AnswerOut(id=row.id, question=row.question, answer=row.answer, field_type=row.field_type,
                     approved=row.approved, origin=row.origin, uses=row.uses, last_used_at=row.last_used_at,
                     updated_at=row.updated_at)


@router.get("", response_model=AnswerList)
def list_answers(approved: Optional[bool] = Query(None), user=Depends(get_current_user),
                 db: Session = Depends(get_db)):
    rows = answer_bank.list_entries(db, user.id, approved)
    return {"answers": [_out(r) for r in rows], "total": len(rows)}


@router.post("", response_model=AnswerOut, status_code=201)
def save_answer(req: AnswerIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = answer_bank.save_user_answer(db, user.id, question=req.question, answer=req.answer,
                                       field_type=req.field_type)
    db.commit()
    return _out(row)


@router.put("/{entry_id}", response_model=AnswerOut)
def update_answer(entry_id: str, req: AnswerUpdate, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = answer_bank.update_entry(answer_bank.get_owned(db, user.id, entry_id), answer=req.answer,
                                   approved=req.approved)
    db.commit()
    return _out(row)


@router.post("/{entry_id}/approve", response_model=AnswerOut)
def approve_answer(entry_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    row = answer_bank.update_entry(answer_bank.get_owned(db, user.id, entry_id), answer=None, approved=True)
    db.commit()
    return _out(row)


@router.delete("/{entry_id}", status_code=204)
def delete_answer(entry_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(answer_bank.get_owned(db, user.id, entry_id))
    db.commit()
