"""
app/models/user.py
--------------------
SQLAlchemy ORM model for user accounts (separate from
app/models/schemas.py, which holds Pydantic request/response models).

Identity (CLAUDE.md 7.1): a user can sign in with a password (legacy),
an emailed one-time code, or Google. `hashed_password` is therefore
optional, and `email_verified_at` records when we last had proof the
user controls the address (OTP or a Google token with email_verified).

Access control (CLAUDE.md 2, 7.11): `role` is user | manager | admin;
`manager_id` links a user to their manager's team. `session_version` is
stamped into every access/refresh token; bumping it invalidates all of
the user's tokens at once (disable, admin "reset sessions", refresh-token
reuse). A counter rather than a timestamp because issue and revoke can
land in the same clock tick (Windows ticks every ~15 ms).
`sessions_revoked_at` records when that last happened, for display.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Integer
from app.core.db import Base

ROLES = ("user", "manager", "admin")


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    name = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, server_default="1")

    role = Column(String(16), default="user", nullable=False, server_default="user")
    manager_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    auth_provider = Column(String(16), default="password", nullable=False, server_default="password")
    google_sub = Column(String(255), unique=True, nullable=True)
    email_verified_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    sessions_revoked_at = Column(DateTime, nullable=True)
    session_version = Column(Integer, default=0, nullable=False, server_default="0")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"
