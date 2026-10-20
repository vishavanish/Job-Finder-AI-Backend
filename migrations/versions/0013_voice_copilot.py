"""Live Copilot (P7b): Deepgram Nova-3 streaming speech-to-text in the registry.

Paid (new accounts get free credit) and it needs a `deepgram` key, so the
router skips it until one is stored; once it can run it comes before Groq
Whisper (priority 5) because only a streaming model gives partial
transcripts (speculative start, < 1 s first words). cost_per_1k_in is the
price per 1,000 audio seconds `[verify before use]`.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ai_models = sa.table(
        "ai_models",
        sa.column("id", sa.String), sa.column("provider", sa.String), sa.column("model_id", sa.String),
        sa.column("display_name", sa.String), sa.column("tier", sa.String), sa.column("tasks", sa.JSON),
        sa.column("base_url", sa.String), sa.column("key_scope", sa.String), sa.column("cost_per_1k_in", sa.Float),
        sa.column("cost_per_1k_out", sa.Float), sa.column("enabled_global", sa.Boolean),
        sa.column("priority", sa.Integer), sa.column("notes", sa.String),
        sa.column("consecutive_failures", sa.Integer), sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.bulk_insert(ai_models, [{
        "id": str(uuid.uuid4()), "provider": "deepgram", "model_id": "nova-3",
        "display_name": "Deepgram Nova-3 (streaming)", "tier": "paid", "tasks": ["stt"], "base_url": None,
        "key_scope": "deepgram", "cost_per_1k_in": 0.128, "cost_per_1k_out": 0.0, "enabled_global": True,
        "priority": 5, "consecutive_failures": 0, "created_at": now, "updated_at": now,
        "notes": "Streaming speech to text with partials and diarization (Live Copilot < 1 s). Needs a deepgram "
                 "key; check the price before use.",
    }])


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM ai_models WHERE provider = 'deepgram' AND model_id = 'nova-3'"))
