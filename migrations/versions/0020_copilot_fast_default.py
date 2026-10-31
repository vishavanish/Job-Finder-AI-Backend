"""Live Copilot speed: Groq answers first.

Admin default for `copilot_answer` and `question_gate` = the Groq GPT-OSS 120B row (one of the fastest
first tokens; free). Set only when the task has no default yet and the row exists; the rest of the
chain (Gemini, ...) stays as fallback. Admins can change it in Admin -> AI models.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-31
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TASKS = ("copilot_answer", "question_gate")
UPDATED_BY = "migration 0020"


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT id FROM ai_models WHERE provider = 'groq' AND model_id = 'openai/gpt-oss-120b'"
                               " ORDER BY priority LIMIT 1")).first()
    if row is None:
        return
    for task in TASKS:
        exists = conn.execute(sa.text("SELECT 1 FROM task_model_defaults WHERE task = :t"), {"t": task}).first()
        if exists is None:
            conn.execute(sa.text("INSERT INTO task_model_defaults (task, model_ref, updated_by, updated_at) "
                                 "VALUES (:t, :m, :by, CURRENT_TIMESTAMP)"),
                         {"t": task, "m": row[0], "by": UPDATED_BY})


def downgrade() -> None:
    conn = op.get_bind()
    for task in TASKS:
        conn.execute(sa.text("DELETE FROM task_model_defaults WHERE task = :t AND updated_by = :by"),
                     {"t": task, "by": UPDATED_BY})
