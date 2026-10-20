"""
app/models/voice.py
---------------------
Voice interview prep (CLAUDE.md 7.10, P7):

  - VoiceSession: one Practice (mode "mock") or Live Copilot (mode "copilot")
    session. Owns its turns and feedback; purged at purge_at.
  - VoiceTurn: one utterance, question or answer, in order. The text is
    sealed (key_store.seal) — transcripts are encrypted at rest.
  - VoiceFeedback: evaluation per answer (turn_id set) or for the whole
    session (turn_id None). The model answer is sealed.
  - StoryBankEntry: the user's STAR stories, used to ground model answers
    and copilot suggestions. Kept until the user deletes them.
  - VoiceSettings: per-user voice choices and the Live Copilot
    acknowledgement (version + time).

Raw audio and screenshots are never stored anywhere.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.core.db import Base
from app.models.user import User  # noqa: F401 — see app/models/application.py for why


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class VoiceSession(Base):
    __tablename__ = "voice_sessions"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    mode = Column(String(8), nullable=False)                     # mock | copilot
    state = Column(String(12), nullable=False, default="created")  # created|active|paused|ended|failed
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    resume_id = Column(String(36), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    role = Column(String(200), nullable=True)                    # typed role when no job is chosen
    config = Column(JSON, nullable=False, default=dict)          # type, difficulty, length, style, language, ...
    providers = Column(JSON, nullable=False, default=dict)       # stt/tts/llm actually used
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)               # last message on the socket (resume window)
    end_reason = Column(String(32), nullable=True)               # user|length|max_minutes|cost_cap|idle|error
    duration_s = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    cost_cap_usd = Column(Float, nullable=False, default=0.0)
    latency = Column(JSON, nullable=False, default=dict)         # p50/p95 per stage
    feedback_state = Column(String(12), nullable=False, default="none")  # none|pending|ready|failed
    overall_score = Column(Float, nullable=True)
    summary_sealed = Column(Text, nullable=True)
    purge_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class VoiceTurn(Base):
    __tablename__ = "voice_turns"

    id = Column(String(36), primary_key=True, default=_uuid)
    session_id = Column(String(36), ForeignKey("voice_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    speaker = Column(String(12), nullable=False)        # interviewer | user | mixed | assistant
    kind = Column(String(12), nullable=False)           # question | answer | suggestion | utterance | closing
    text_sealed = Column(Text, nullable=False)
    question_id = Column(String(36), nullable=True)     # copilot: the card it belongs to
    parent_id = Column(String(36), nullable=True)       # follow-ups: the earlier question
    qtype = Column(String(24), nullable=True)
    is_followup = Column(Boolean, nullable=False, default=False)
    state = Column(String(12), nullable=False, default="shown")  # shown|interrupted|cancelled|dismissed|skipped
    not_sure = Column(Boolean, nullable=False, default=False)
    start_ms = Column(Integer, nullable=True)
    end_ms = Column(Integer, nullable=True)
    metrics = Column(JSON, nullable=False, default=dict)  # wpm, fillers, pauses (numbers only, never audio)
    latency = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=_now, nullable=False)


class VoiceFeedback(Base):
    __tablename__ = "voice_feedback"

    id = Column(String(36), primary_key=True, default=_uuid)
    session_id = Column(String(36), ForeignKey("voice_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey("voice_turns.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(String, nullable=False, index=True)
    scores = Column(JSON, nullable=False, default=dict)   # accuracy, relevance, structure, clarity, delivery (1-5)
    skill_area = Column(String(64), nullable=True)
    star = Column(JSON, nullable=False, default=dict)     # {situation, task, action, result: bool}
    strengths = Column(JSON, nullable=False, default=list)
    improvements = Column(JSON, nullable=False, default=list)
    model_answer_sealed = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class StoryBankEntry(Base):
    __tablename__ = "story_bank"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    situation = Column(Text, nullable=False, default="")
    task = Column(Text, nullable=False, default="")
    action = Column(Text, nullable=False, default="")
    result = Column(Text, nullable=False, default="")
    skills = Column(JSON, nullable=False, default=list)
    origin = Column(String(8), nullable=False, default="user")  # user | ai
    approved = Column(Boolean, nullable=False, default=True)    # AI drafts start unapproved
    uses = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class VoiceSettings(Base):
    __tablename__ = "voice_settings"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    copilot_ack_version = Column(String(32), nullable=True)
    copilot_ack_at = Column(DateTime, nullable=True)
    answer_style = Column(String(12), nullable=False, default="bullets")  # bullets | script | star
    answer_length = Column(String(8), nullable=False, default="short")    # short | medium | long
    language = Column(String(16), nullable=False, default="en")
    tts_voice = Column(String(100), nullable=True)
    default_sources = Column(String(12), nullable=False, default="tab_mic")  # tab_mic | mic
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
