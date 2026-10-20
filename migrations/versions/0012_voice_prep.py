"""Voice interview prep (P7): voice_sessions, voice_turns, voice_feedback,
story_bank, voice_settings; speech models in the registry.

Seeded ai_models rows:
  - Groq Whisper (speech to text, free key) and "Browser voice" (text to
    speech done by the user's browser — free, no key);
  - OpenRouter (OpenAI-compatible) as the last fallback for every text task,
    switched OFF until the owner stores an OpenRouter key and turns it on;
  - OpenAI (ChatGPT) text, speech-to-text and voice rows, switched OFF until
    an OpenAI key exists.
Speech rows count audio seconds (STT) or characters (TTS) in tokens_in, so
cost_per_1k_in is the price per 1,000 seconds / characters.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

SEEDED = (
    ("groq", "whisper-large-v3-turbo", None),
    ("browser", "speech-synthesis", None),
    ("openai_compatible", "openai/gpt-oss-120b:free", "https://openrouter.ai/api/v1"),
    ("openai_compatible", "gpt-5-mini", "https://api.openai.com/v1"),
    ("openai_compatible", "gpt-4o-mini-transcribe", "https://api.openai.com/v1"),
    ("openai_compatible", "gpt-4o-mini-tts", "https://api.openai.com/v1"),
)


def upgrade() -> None:
    op.create_table(
        "voice_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resume_id", sa.String(36), sa.ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("role", sa.String(200), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("providers", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("end_reason", sa.String(32), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("cost_cap_usd", sa.Float(), nullable=False),
        sa.Column("latency", sa.JSON(), nullable=False),
        sa.Column("feedback_state", sa.String(12), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("summary_sealed", sa.Text(), nullable=True),
        sa.Column("purge_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_voice_sessions_user_id", "voice_sessions", ["user_id"])
    op.create_index("ix_voice_sessions_purge_at", "voice_sessions", ["purge_at"])

    op.create_table(
        "voice_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("voice_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(12), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("text_sealed", sa.Text(), nullable=False),
        sa.Column("question_id", sa.String(36), nullable=True),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("qtype", sa.String(24), nullable=True),
        sa.Column("is_followup", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("not_sure", sa.Boolean(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms", sa.Integer(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("latency", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_voice_turns_session_id", "voice_turns", ["session_id"])
    op.create_index("ix_voice_turns_user_id", "voice_turns", ["user_id"])

    op.create_table(
        "voice_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("voice_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("turn_id", sa.String(36), sa.ForeignKey("voice_turns.id", ondelete="CASCADE"), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("skill_area", sa.String(64), nullable=True),
        sa.Column("star", sa.JSON(), nullable=False),
        sa.Column("strengths", sa.JSON(), nullable=False),
        sa.Column("improvements", sa.JSON(), nullable=False),
        sa.Column("model_answer_sealed", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_voice_feedback_session_id", "voice_feedback", ["session_id"])
    op.create_index("ix_voice_feedback_user_id", "voice_feedback", ["user_id"])

    op.create_table(
        "story_bank",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("situation", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_story_bank_user_id", "story_bank", ["user_id"])

    op.create_table(
        "voice_settings",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("copilot_ack_version", sa.String(32), nullable=True),
        sa.Column("copilot_ack_at", sa.DateTime(), nullable=True),
        sa.Column("answer_style", sa.String(12), nullable=False),
        sa.Column("answer_length", sa.String(8), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("tts_voice", sa.String(100), nullable=True),
        sa.Column("default_sources", sa.String(12), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    ai_models = sa.table(
        "ai_models",
        sa.column("id", sa.String), sa.column("provider", sa.String), sa.column("model_id", sa.String),
        sa.column("display_name", sa.String), sa.column("tier", sa.String), sa.column("tasks", sa.JSON),
        sa.column("base_url", sa.String), sa.column("key_scope", sa.String), sa.column("cost_per_1k_in", sa.Float),
        sa.column("cost_per_1k_out", sa.Float), sa.column("context_window", sa.Integer),
        sa.column("max_output_tokens", sa.Integer), sa.column("enabled_global", sa.Boolean),
        sa.column("priority", sa.Integer), sa.column("notes", sa.String),
        sa.column("consecutive_failures", sa.Integer), sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = {"tier": "free", "tasks": [], "base_url": None, "cost_per_1k_in": 0.0, "cost_per_1k_out": 0.0,
            "context_window": None, "max_output_tokens": None, "enabled_global": True, "notes": None,
            "consecutive_failures": 0, "created_by": None, "created_at": now, "updated_at": now}
    openai = {"base_url": "https://api.openai.com/v1", "key_scope": "openai", "tier": "paid",
              "enabled_global": False}
    op.bulk_insert(ai_models, [
        {**base, "id": str(uuid.uuid4()), "provider": "groq", "model_id": "whisper-large-v3-turbo",
         "display_name": "Whisper Large v3 Turbo (Groq)", "key_scope": "groq", "tasks": ["stt"], "priority": 10,
         "cost_per_1k_in": 0.0111, "notes": "Speech to text, one utterance at a time. Free tier with rate limits."},
        {**base, "id": str(uuid.uuid4()), "provider": "browser", "model_id": "speech-synthesis",
         "display_name": "Browser voice", "key_scope": "none", "tasks": ["tts"], "priority": 10,
         "notes": "The user's browser reads the interviewer's words aloud. Free, no key."},
        {**base, "id": str(uuid.uuid4()), "provider": "openai_compatible", "model_id": "openai/gpt-oss-120b:free",
         "display_name": "GPT-OSS 120B (OpenRouter, fallback)", "base_url": "https://openrouter.ai/api/v1",
         "key_scope": "openrouter", "priority": 900, "enabled_global": False,
         "notes": "Last fallback for every text task. Store an OpenRouter platform key, check the model name, "
                  "then switch it on."},
        {**base, **openai, "id": str(uuid.uuid4()), "model_id": "gpt-5-mini", "provider": "openai_compatible",
         "display_name": "GPT-5 mini (OpenAI)", "priority": 200,
         "notes": "ChatGPT model. Add an OpenAI key, check the model name and prices, then switch it on."},
        {**base, **openai, "id": str(uuid.uuid4()), "model_id": "gpt-4o-mini-transcribe",
         "provider": "openai_compatible", "display_name": "GPT-4o mini Transcribe (OpenAI)", "tasks": ["stt"],
         "priority": 50, "notes": "Speech to text. Add an OpenAI key, then switch it on."},
        {**base, **openai, "id": str(uuid.uuid4()), "model_id": "gpt-4o-mini-tts", "provider": "openai_compatible",
         "display_name": "GPT-4o mini TTS (OpenAI)", "tasks": ["tts"], "priority": 50,
         "notes": "Interviewer voice. Add an OpenAI key, then switch it on."},
    ])


def downgrade() -> None:
    for provider, model_id, base_url in SEEDED:
        cond = "base_url IS NULL" if base_url is None else "base_url = :b"
        op.get_bind().execute(
            sa.text(f"DELETE FROM ai_models WHERE provider = :p AND model_id = :m AND {cond}"),
            {"p": provider, "m": model_id, "b": base_url},
        )
    op.drop_table("voice_settings")
    op.drop_table("story_bank")
    op.drop_index("ix_voice_feedback_user_id", "voice_feedback")
    op.drop_index("ix_voice_feedback_session_id", "voice_feedback")
    op.drop_table("voice_feedback")
    op.drop_index("ix_voice_turns_user_id", "voice_turns")
    op.drop_index("ix_voice_turns_session_id", "voice_turns")
    op.drop_table("voice_turns")
    op.drop_index("ix_voice_sessions_purge_at", "voice_sessions")
    op.drop_index("ix_voice_sessions_user_id", "voice_sessions")
    op.drop_table("voice_sessions")
