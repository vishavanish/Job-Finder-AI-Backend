"""Generic AI apply (P4c): the AI form reader for career sites without their own adapter.

apply_settings.allow_generic (the user's opt-in, off by default) and the new router task form_vision on the
models that read screenshots: the Gemini rows and OpenAI GPT-5 mini (form_vision is served only by rows that
list it; listing it keeps every other text task). form_map needs nothing: rows with tasks == [] serve it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-26
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_SPEECH = {"stt", "tts"}
_models = sa.table("ai_models", sa.column("id", sa.String), sa.column("provider", sa.String),
                   sa.column("model_id", sa.String), sa.column("tasks", sa.JSON))


def _vision_rows(conn) -> list[tuple[str, list]]:
    rows = conn.execute(sa.select(_models.c.id, _models.c.provider, _models.c.model_id, _models.c.tasks)).fetchall()
    out = []
    for row_id, provider, model_id, tasks in rows:
        tasks = json.loads(tasks) if isinstance(tasks, str) else list(tasks or [])
        if set(tasks) & _SPEECH:
            continue
        if provider == "gemini" or (provider == "openai_compatible" and model_id == "gpt-5-mini"):
            out.append((row_id, tasks))
    return out


def upgrade() -> None:
    with op.batch_alter_table("apply_settings") as batch:
        batch.add_column(sa.Column("allow_generic", sa.Boolean(), nullable=False, server_default=sa.false()))
    conn = op.get_bind()
    for row_id, tasks in _vision_rows(conn):
        if "form_vision" not in tasks:
            conn.execute(_models.update().where(_models.c.id == row_id).values(tasks=tasks + ["form_vision"]))


def downgrade() -> None:
    conn = op.get_bind()
    for row_id, tasks in _vision_rows(conn):
        if "form_vision" in tasks:
            conn.execute(_models.update().where(_models.c.id == row_id)
                         .values(tasks=[t for t in tasks if t != "form_vision"]))
    with op.batch_alter_table("apply_settings") as batch:
        batch.drop_column("allow_generic")
