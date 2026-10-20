"""
app/services/voice_store.py
-----------------------------
Voice sessions, turns, feedback and settings in the database (P7).

Every text a user or model says is sealed (key_store.seal) before it is
written and unsealed only for its owner. Raw audio never reaches this
module. Sessions are deleted VOICE_TRANSCRIPT_RETENTION_DAYS after they end
(purge_expired, daily) or at once by the user (delete_session / delete_all).
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.auth import as_utc
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.jobs import Job
from app.models.voice import VoiceFeedback, VoiceSession, VoiceSettings, VoiceTurn
from app.services import key_store, resume_store

COPILOT_ACK_TEXT = (
    "Some employers and interview platforms prohibit AI assistance during interviews, and using it where it "
    "is not allowed can get you disqualified. You are responsible for following the rules of each interview. "
    "Live Copilot stays visible on your screen and only listens while you allow it. Good uses: practice with "
    "recorded or mock calls, meetings and learning, accessibility, and interviews where AI help is permitted."
)

LIVE_STATES = ("created", "active", "paused")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def api_error(code: int, err: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": err, "message": message})


def seal(text: str) -> str:
    return key_store.seal(text or "")


def unseal(sealed: str | None) -> str:
    if not sealed:
        return ""
    try:
        return key_store.unseal(sealed)
    except RuntimeError:
        return ""  # master key rotated: the text can't be read any more


# ---------------------------------------------------------------- settings

def get_voice_settings(db: Session, user_id: str) -> VoiceSettings:
    row = db.get(VoiceSettings, user_id)
    if row is None:
        row = VoiceSettings(user_id=user_id, answer_style="bullets", answer_length="short", language="en",
                            default_sources="tab_mic")
    return row


def settings_out(row: VoiceSettings) -> dict:
    s = get_settings()
    return {
        "answer_style": row.answer_style or "bullets", "answer_length": row.answer_length or "short",
        "language": row.language or "en", "tts_voice": row.tts_voice,
        "default_sources": row.default_sources or "tab_mic",
        "copilot_ack_version": s.COPILOT_ACK_VERSION, "copilot_ack_text": COPILOT_ACK_TEXT,
        "copilot_acknowledged": row.copilot_ack_version == s.COPILOT_ACK_VERSION,
        "copilot_ack_at": row.copilot_ack_at, "transcript_retention_days": s.VOICE_TRANSCRIPT_RETENTION_DAYS,
    }


def update_settings(db: Session, user_id: str, changes: dict) -> VoiceSettings:
    row = get_voice_settings(db, user_id)
    if db.get(VoiceSettings, user_id) is None:
        db.add(row)
    for key, value in changes.items():
        if value is not None:
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------- sessions

def get_owned(db: Session, user_id: str, session_id: str) -> VoiceSession:
    row = db.query(VoiceSession).filter(VoiceSession.id == session_id, VoiceSession.user_id == user_id).first()
    if row is None:
        raise api_error(404, "voice_session_not_found", "No voice session with this id.")
    return row


def create_session(db: Session, user_id: str, *, mode: str, job_id: str | None, role: str | None,
                   config: dict) -> VoiceSession:
    s = get_settings()
    job = None
    if job_id:
        job = db.get(Job, job_id)
        if job is None:
            raise api_error(404, "job_not_found", "No job with this id.")
    if not job and not (role or "").strip() and config.get("interview_type") == "role_specific":
        raise api_error(422, "voice_role_required", "Pick a job or type the role for a role-specific interview.")
    master = resume_store.master(db, user_id)
    cap = s.VOICE_PRACTICE_COST_CAP_USD if mode == "mock" else s.VOICE_COPILOT_COST_CAP_USD
    row = VoiceSession(user_id=user_id, mode=mode, state="created", job_id=job.id if job else None,
                       resume_id=master.id if master else None, role=(role or "").strip() or None, config=config,
                       providers={}, duration_s=0, cost_usd=0.0, cost_cap_usd=cap, latency={},
                       feedback_state="none")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def session_out(db: Session, row: VoiceSession) -> dict:
    job = db.get(Job, row.job_id) if row.job_id else None
    questions = (db.query(VoiceTurn).filter(VoiceTurn.session_id == row.id, VoiceTurn.speaker == "interviewer",
                                            VoiceTurn.kind == "question").count())
    return {
        "id": row.id, "mode": row.mode, "state": row.state, "job_id": row.job_id,
        "job_title": job.title if job else None, "company": job.company if job else None, "role": row.role,
        "config": row.config or {}, "providers": row.providers or {}, "started_at": row.started_at,
        "ended_at": row.ended_at, "end_reason": row.end_reason, "duration_s": row.duration_s or 0,
        "cost_usd": round(row.cost_usd or 0.0, 4), "cost_cap_usd": row.cost_cap_usd or 0.0,
        "feedback_state": row.feedback_state, "overall_score": row.overall_score, "question_count": questions,
        "created_at": row.created_at,
    }


def turn_out(t: VoiceTurn) -> dict:
    return {"id": t.id, "seq": t.seq, "speaker": t.speaker, "kind": t.kind, "text": unseal(t.text_sealed),
            "question_id": t.question_id, "parent_id": t.parent_id, "is_followup": bool(t.is_followup),
            "state": t.state, "start_ms": t.start_ms, "end_ms": t.end_ms, "metrics": t.metrics or {},
            "latency": t.latency or {}}


def turns(db: Session, session_id: str) -> list[VoiceTurn]:
    return db.query(VoiceTurn).filter(VoiceTurn.session_id == session_id).order_by(VoiceTurn.seq).all()


def history(db: Session, user_id: str, *, mode: str | None, limit: int, offset: int) -> tuple[list[VoiceSession], int]:
    q = db.query(VoiceSession).filter(VoiceSession.user_id == user_id)
    if mode:
        q = q.filter(VoiceSession.mode == mode)
    total = q.count()
    return q.order_by(VoiceSession.created_at.desc()).offset(offset).limit(limit).all(), total


def delete_session(db: Session, row: VoiceSession) -> None:
    db.query(VoiceFeedback).filter(VoiceFeedback.session_id == row.id).delete(synchronize_session=False)
    db.query(VoiceTurn).filter(VoiceTurn.session_id == row.id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()


def delete_all(db: Session, user_id: str) -> int:
    ids = [r[0] for r in db.query(VoiceSession.id).filter(VoiceSession.user_id == user_id).all()]
    if ids:
        db.query(VoiceFeedback).filter(VoiceFeedback.session_id.in_(ids)).delete(synchronize_session=False)
        db.query(VoiceTurn).filter(VoiceTurn.session_id.in_(ids)).delete(synchronize_session=False)
        db.query(VoiceSession).filter(VoiceSession.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return len(ids)


def purge_expired(db: Session) -> int:
    """Deletes sessions whose retention ended, and abandoned ones never
    started within a day."""
    now = _naive(_now())
    expired = [r[0] for r in db.query(VoiceSession.id).filter(VoiceSession.purge_at.isnot(None),
                                                               VoiceSession.purge_at <= now).all()]
    stale = [r[0] for r in db.query(VoiceSession.id).filter(VoiceSession.state == "created",
                                                             VoiceSession.created_at <= now - timedelta(days=1))]
    ids = expired + stale
    if ids:
        db.query(VoiceFeedback).filter(VoiceFeedback.session_id.in_(ids)).delete(synchronize_session=False)
        db.query(VoiceTurn).filter(VoiceTurn.session_id.in_(ids)).delete(synchronize_session=False)
        db.query(VoiceSession).filter(VoiceSession.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return len(ids)


# ---------------------------------------------------------------- writes from the socket (own DB session each)

def add_turn(*, session_id: str, user_id: str, seq: int, speaker: str, kind: str, text: str,
             turn_id: str | None = None, state: str = "shown", question_id: str | None = None,
             parent_id: str | None = None, is_followup: bool = False, start_ms: int | None = None,
             end_ms: int | None = None, metrics: dict | None = None, latency: dict | None = None) -> str:
    db = SessionLocal()
    try:
        row = VoiceTurn(id=turn_id, session_id=session_id, user_id=user_id, seq=seq, speaker=speaker, kind=kind,
                        text_sealed=seal(text), state=state, question_id=question_id, parent_id=parent_id,
                        is_followup=is_followup, not_sure=False, start_ms=start_ms, end_ms=end_ms,
                        metrics=metrics or {}, latency=latency or {})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def set_turn_state(turn_id: str, state: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(VoiceTurn, turn_id)
        if row is not None:
            row.state = state
            db.commit()
    finally:
        db.close()


def mark_started(session_id: str, providers: dict) -> None:
    db = SessionLocal()
    try:
        row = db.get(VoiceSession, session_id)
        if row is None:
            return
        now = _naive(_now())
        if row.started_at is None:
            row.started_at = now
        row.state = "active"
        row.last_seen_at = now
        row.providers = providers
        db.commit()
    finally:
        db.close()


def touch(session_id: str, *, state: str | None = None, cost_usd: float | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(VoiceSession, session_id)
        if row is None:
            return
        row.last_seen_at = _naive(_now())
        if state and row.state in LIVE_STATES:
            row.state = state
        if cost_usd is not None:
            row.cost_usd = round(cost_usd, 6)
        db.commit()
    finally:
        db.close()


def end_session(session_id: str, *, reason: str, cost_usd: float | None = None) -> bool:
    """Marks the session ended (once) and sets its purge date. True when
    this call ended it."""
    db = SessionLocal()
    try:
        row = db.get(VoiceSession, session_id)
        if row is None or row.state in ("ended", "failed"):
            return False
        now = _now()
        row.state = "ended"
        row.end_reason = reason
        row.ended_at = _naive(now)
        if cost_usd is not None:
            row.cost_usd = round(cost_usd, 6)
        started = as_utc(row.started_at)
        row.duration_s = int((now - started).total_seconds()) if started else 0
        row.purge_at = _naive(now + timedelta(days=get_settings().VOICE_TRANSCRIPT_RETENTION_DAYS))
        row.feedback_state = "pending"
        db.commit()
        return True
    finally:
        db.close()


# ---------------------------------------------------------------- progress

def progress(db: Session, user_id: str) -> dict:
    sessions = (db.query(VoiceSession).filter(VoiceSession.user_id == user_id, VoiceSession.mode == "mock",
                                              VoiceSession.feedback_state == "ready")
                .order_by(VoiceSession.ended_at).all())
    by_id = {s.id: s for s in sessions}
    rows = (db.query(VoiceFeedback).filter(VoiceFeedback.user_id == user_id, VoiceFeedback.turn_id.isnot(None),
                                           VoiceFeedback.session_id.in_(list(by_id) or [""])).all())
    areas: dict[str, list[tuple[VoiceSession, float]]] = {}
    weak: list[tuple[float, str]] = []
    for fb in rows:
        avg = _avg_score(fb.scores)
        if avg is None:
            continue
        areas.setdefault(fb.skill_area or "general", []).append((by_id[fb.session_id], avg))
        if avg < 3:
            turn = db.get(VoiceTurn, fb.turn_id)
            question = _question_for(db, turn) if turn else ""
            if question:
                weak.append((avg, question))
    out_areas = []
    for area, items in sorted(areas.items()):
        per_session: dict[str, list[float]] = {}
        for sess, score in items:
            per_session.setdefault(sess.id, []).append(score)
        trend = [{"session_id": sid, "date": by_id[sid].ended_at or by_id[sid].created_at,
                  "score": round(statistics.mean(v), 2)} for sid, v in per_session.items()]
        trend.sort(key=lambda p: p["date"])
        out_areas.append({"skill_area": area, "answers": len(items),
                          "average": round(statistics.mean(s for _, s in items), 2), "trend": trend})
    by_type: dict[str, dict] = {}
    for s in sessions:
        if s.overall_score is None:
            continue
        t = (s.config or {}).get("interview_type", "behavioral")
        entry = by_type.setdefault(t, {"sessions": 0, "average": 0.0, "_scores": []})
        entry["sessions"] += 1
        entry["_scores"].append(s.overall_score)
    for entry in by_type.values():
        entry["average"] = round(statistics.mean(entry.pop("_scores")), 2)
    scores = [s.overall_score for s in sessions if s.overall_score is not None]
    weak.sort()
    seen: set[str] = set()
    weak_questions = [q for _, q in weak if not (q in seen or seen.add(q))][:10]
    return {"sessions": len(sessions), "average": round(statistics.mean(scores), 2) if scores else None,
            "areas": out_areas, "by_type": by_type, "weak_questions": weak_questions}


def _avg_score(scores: dict | None) -> float | None:
    values = [float(v) for v in (scores or {}).values() if isinstance(v, (int, float))]
    return statistics.mean(values) if values else None


def _question_for(db: Session, answer: VoiceTurn) -> str:
    q = (db.query(VoiceTurn).filter(VoiceTurn.session_id == answer.session_id, VoiceTurn.seq < answer.seq,
                                    VoiceTurn.speaker == "interviewer")
         .order_by(VoiceTurn.seq.desc()).first())
    return unseal(q.text_sealed) if q else ""
