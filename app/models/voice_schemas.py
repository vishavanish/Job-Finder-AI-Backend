"""
app/models/voice_schemas.py
-----------------------------
Request/response models for voice interview prep and the story bank (P7).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

InterviewType = Literal["behavioral", "technical", "system_design", "role_specific", "rapid_fire",
                        "resume_walkthrough"]
Difficulty = Literal["easy", "medium", "hard"]
AnswerStyle = Literal["bullets", "script", "star"]
AnswerLength = Literal["short", "medium", "long"]


class VoiceSettingsOut(BaseModel):
    answer_style: AnswerStyle
    answer_length: AnswerLength
    language: str
    tts_voice: Optional[str] = None
    default_sources: Literal["tab_mic", "mic"]
    copilot_ack_version: str = Field(..., description="Current acknowledgement version")
    copilot_ack_text: str
    copilot_acknowledged: bool = Field(..., description="True when the user accepted the CURRENT version")
    copilot_ack_at: Optional[datetime] = None
    transcript_retention_days: int


class VoiceSettingsIn(BaseModel):
    answer_style: Optional[AnswerStyle] = None
    answer_length: Optional[AnswerLength] = None
    language: Optional[str] = Field(None, min_length=2, max_length=16)
    tts_voice: Optional[str] = Field(None, max_length=100)
    default_sources: Optional[Literal["tab_mic", "mic"]] = None


class SpeechModelOut(BaseModel):
    name: str
    provider: str
    model_id: str
    tier: str
    kind: Optional[str] = None


class VoiceProvidersOut(BaseModel):
    stt: Optional[SpeechModelOut] = None
    tts: SpeechModelOut
    llm_fast: Optional[str] = None
    llm_strong: Optional[str] = None
    streaming_stt: bool
    diarization: bool
    expected_first_word_ms: int
    ready: bool = Field(..., description="False when no speech-to-text model can run (see reason)")
    reason: Optional[str] = None


class SessionCreateIn(BaseModel):
    mode: Literal["mock"] = "mock"
    job_id: Optional[str] = None
    role: Optional[str] = Field(None, max_length=200, description="Typed role when no job is chosen")
    interview_type: InterviewType = "behavioral"
    difficulty: Difficulty = "medium"
    length: int = Field(5, ge=1, le=20, description="Main questions (follow-ups are extra)")
    language: Optional[str] = Field(None, max_length=16)
    focus_questions: list[str] = Field(default_factory=list, max_length=20,
                                       description="Questions to drill (e.g. weak ones from an earlier session)")


class VoiceSessionOut(BaseModel):
    id: str
    mode: str
    state: str
    job_id: Optional[str] = None
    job_title: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    config: dict
    providers: dict
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    end_reason: Optional[str] = None
    duration_s: int
    cost_usd: float
    cost_cap_usd: float
    feedback_state: str
    overall_score: Optional[float] = None
    question_count: int = 0
    created_at: datetime


class SessionCreateOut(BaseModel):
    session: VoiceSessionOut
    stream_token: str = Field(..., description="Single-use; send as the first socket message {type:'auth', token}")
    stream_path: str
    expires_in: int


class StreamTokenOut(BaseModel):
    stream_token: str
    expires_in: int


class VoiceTurnOut(BaseModel):
    id: str
    seq: int
    speaker: str
    kind: str
    text: str
    question_id: Optional[str] = None
    parent_id: Optional[str] = None
    is_followup: bool = False
    state: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    metrics: dict = Field(default_factory=dict)
    latency: dict = Field(default_factory=dict)


class VoiceSessionDetail(BaseModel):
    session: VoiceSessionOut
    turns: list[VoiceTurnOut]


class AnswerFeedbackOut(BaseModel):
    turn_id: Optional[str] = None
    question: str
    answer: str
    skill_area: Optional[str] = None
    scores: dict
    star: dict
    strengths: list[str]
    improvements: list[str]
    model_answer: Optional[str] = None
    metrics: dict = Field(default_factory=dict)


class VoiceFeedbackOut(BaseModel):
    state: str = Field(..., description="none | pending | ready | failed")
    overall_score: Optional[float] = None
    summary: Optional[str] = None
    scores: dict = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    weak_questions: list[str] = Field(default_factory=list)
    delivery: dict = Field(default_factory=dict)
    answers: list[AnswerFeedbackOut] = Field(default_factory=list)


class VoiceHistoryOut(BaseModel):
    sessions: list[VoiceSessionOut]
    total: int


class ProgressPoint(BaseModel):
    session_id: str
    date: datetime
    score: float


class ProgressArea(BaseModel):
    skill_area: str
    answers: int
    average: float
    trend: list[ProgressPoint]


class VoiceProgressOut(BaseModel):
    sessions: int
    average: Optional[float] = None
    areas: list[ProgressArea]
    by_type: dict
    weak_questions: list[str]


class StoryIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    situation: str = Field("", max_length=3000)
    task: str = Field("", max_length=3000)
    action: str = Field("", max_length=5000)
    result: str = Field("", max_length=3000)
    skills: list[str] = Field(default_factory=list, max_length=20)


class StoryOut(StoryIn):
    id: str
    origin: str
    approved: bool
    uses: int
    created_at: datetime
    updated_at: datetime


class StoryList(BaseModel):
    stories: list[StoryOut]
    total: int
    needs_review: int


class TaskQueuedOut(BaseModel):
    task_id: str
    message: str
