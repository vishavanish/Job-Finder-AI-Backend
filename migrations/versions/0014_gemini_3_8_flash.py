"""Gemini 2.5 Flash -> Gemini 3.8 Flash in the model registry.

Google stopped serving gemini-2.5-flash to new accounts (404 "no longer
available to new users"), so every scoring call fell through to Groq / HF.
Only the seeded row is renamed: an admin's own edits to other rows stay.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _rename(old_id: str, new_id: str, new_name: str) -> None:
    op.get_bind().execute(
        sa.text("UPDATE ai_models SET model_id = :new_id, display_name = :new_name, consecutive_failures = 0 "
                "WHERE provider = 'gemini' AND model_id = :old_id"),
        {"old_id": old_id, "new_id": new_id, "new_name": new_name},
    )


def upgrade() -> None:
    _rename("gemini-2.5-flash", "gemini-3.8-flash", "Gemini 3.8 Flash")


def downgrade() -> None:
    _rename("gemini-3.8-flash", "gemini-2.5-flash", "Gemini 2.5 Flash")
