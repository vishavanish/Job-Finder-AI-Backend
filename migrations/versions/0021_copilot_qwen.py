"""Live Copilot: Qwen 3.8 27B on Groq answers first.

Measured 2026-10-31 on five interview questions (coding, DevOps, frontend, backend): first words 0.22 s
median vs 0.59 s for GPT-OSS 120B, with the same code quality. New row "Qwen 3.8 27B (Groq)" (free, Groq
key) serving only `copilot_answer` and `question_gate`; it becomes the admin default for both where the
default is still the one migration 0020 set (an admin's own choice is kept). GPT-OSS 120B and the rest of
the chain stay as fallbacks.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-31
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

MODEL_ID = "qwen/qwen3.8-27b"
TASKS = ("copilot_answer", "question_gate")
UPDATED_BY = "migration 0021"
PREVIOUS = "migration 0020"

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


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT id FROM ai_models WHERE provider = 'groq' AND model_id = :m"),
                       {"m": MODEL_ID}).first()
    if row is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        model_ref = str(uuid.uuid4())
        op.bulk_insert(ai_models, [{
            "id": model_ref, "provider": "groq", "model_id": MODEL_ID, "display_name": "Qwen 3.8 27B (Groq)",
            "tier": "free", "tasks": list(TASKS), "base_url": None, "key_scope": "groq", "cost_per_1k_in": 0.0,
            "cost_per_1k_out": 0.0, "context_window": None, "max_output_tokens": None, "enabled_global": True,
            "priority": 25, "notes": "Fastest first words for Live Copilot answers (free tier with rate limits).",
            "consecutive_failures": 0, "created_by": None, "created_at": now, "updated_at": now,
        }])
    else:
        model_ref = row[0]
    for task in TASKS:
        current = conn.execute(sa.text("SELECT updated_by FROM task_model_defaults WHERE task = :t"),
                               {"t": task}).first()
        if current is None:
            conn.execute(sa.text("INSERT INTO task_model_defaults (task, model_ref, updated_by, updated_at) "
                                 "VALUES (:t, :m, :by, CURRENT_TIMESTAMP)"),
                         {"t": task, "m": model_ref, "by": UPDATED_BY})
        elif current[0] == PREVIOUS:
            conn.execute(sa.text("UPDATE task_model_defaults SET model_ref = :m, updated_by = :by, "
                                 "updated_at = CURRENT_TIMESTAMP WHERE task = :t"),
                         {"t": task, "m": model_ref, "by": UPDATED_BY})


def downgrade() -> None:
    conn = op.get_bind()
    gpt = conn.execute(sa.text("SELECT id FROM ai_models WHERE provider = 'groq' AND model_id = 'openai/gpt-oss-120b'"
                               " ORDER BY priority LIMIT 1")).first()
    for task in TASKS:
        if gpt is not None:
            conn.execute(sa.text("UPDATE task_model_defaults SET model_ref = :m, updated_by = :by "
                                 "WHERE task = :t AND updated_by = :mine"),
                         {"t": task, "m": gpt[0], "by": PREVIOUS, "mine": UPDATED_BY})
        else:
            conn.execute(sa.text("DELETE FROM task_model_defaults WHERE task = :t AND updated_by = :mine"),
                         {"t": task, "mine": UPDATED_BY})
    # Switched off rather than deleted: usage-ledger rows may point at it.
    conn.execute(sa.text("UPDATE ai_models SET enabled_global = :off WHERE provider = 'groq' AND model_id = :m"),
                 {"m": MODEL_ID, "off": False})
