"""
app/services/audit.py
------------------------
Append-only audit trail (CLAUDE.md 7.11, 12). record() only ADDS the
row to the caller's session — the caller commits it together with the
change it describes, so a change and its audit entry are written in one
transaction or not at all.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.admin import AuditLog


def client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def record(
    db: Session,
    *,
    actor_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        ip=client_ip(request),
    )
    db.add(entry)
    return entry
