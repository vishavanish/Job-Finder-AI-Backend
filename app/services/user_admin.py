"""
app/services/user_admin.py
-----------------------------
Business rules behind the admin console (CLAUDE.md 2, 7.11). Routes in
app/api/routes/admin.py stay thin and call these.

  - Admins manage everyone; managers only the users on their team
    (users.manager_id == manager), and never other managers or admins.
  - Nobody can disable or demote themselves, and the last active admin
    can't be disabled or demoted — the console can't lock itself out.
  - Disabling a user revokes all their sessions and cancels their
    running tasks and streaming runs.
  - Every change writes an audit entry in the same transaction.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from app.core.celery_app import celery_app
from app.core.task_dispatch import task_ids_for_user
from app.models.user import ROLES, User
from app.services import audit, pipeline_progress, sessions
from app.services.otp import normalize_email

logger = logging.getLogger("job_finder_api.user_admin")


def snapshot(user: User) -> dict:
    return {"email": user.email, "name": user.name, "role": user.role,
            "is_active": user.is_active, "manager_id": user.manager_id}


def visible_users(db: Session, actor: User) -> Query:
    q = db.query(User)
    if actor.role != "admin":
        q = q.filter(User.manager_id == actor.id)
    return q


def search_users(db: Session, actor: User, *, q: str | None, role: str | None, active: bool | None,
                 limit: int, offset: int) -> tuple[list[User], int]:
    query = visible_users(db, actor)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(or_(User.email.ilike(like), User.name.ilike(like)))
    if role:
        query = query.filter(User.role == role)
    if active is not None:
        query = query.filter(User.is_active == active)
    total = query.count()
    rows = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    return rows, total


def get_manageable(db: Session, actor: User, user_id: str) -> User:
    """The target user if `actor` may manage them, else 404 (so managers
    can't probe for users outside their team)."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no user found with this id")
    if actor.role == "admin":
        return user
    if actor.role == "manager" and user.manager_id == actor.id and user.role == "user":
        return user
    raise HTTPException(status.HTTP_404_NOT_FOUND, "no user found with this id")


def _active_admin_count(db: Session) -> int:
    return db.query(User).filter(User.role == "admin", User.is_active.is_(True)).count()


def _guard_last_admin(db: Session, target: User) -> None:
    if target.role == "admin" and target.is_active and _active_admin_count(db) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This is the last active admin; add another admin first.")


def _validate_manager(db: Session, manager_id: str | None) -> None:
    if manager_id is None:
        return
    manager = db.query(User).filter(User.id == manager_id).first()
    if manager is None or manager.role not in ("manager", "admin"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manager_id must be an existing manager or admin.")


def create_user(db: Session, actor: User, *, email: str, name: str | None, role: str,
                manager_id: str | None, request: Request) -> User:
    email = normalize_email(email)
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")
    _validate_manager(db, manager_id)
    user = User(email=email, name=name, role=role, manager_id=manager_id, auth_provider="invited")
    db.add(user)
    db.flush()
    audit.record(db, actor_id=actor.id, action="user.create", target_type="user", target_id=user.id,
                 after=snapshot(user), request=request)
    db.commit()
    db.refresh(user)
    return user


def update_user(db: Session, actor: User, target: User, changes: dict, request: Request) -> User:
    """Admin-only edits: name, role, manager_id."""
    before = snapshot(target)
    if "role" in changes and changes["role"] != target.role:
        if changes["role"] not in ROLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of {ROLES}")
        if target.id == actor.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't change your own role.")
        _guard_last_admin(db, target)
        target.role = changes["role"]
    if "manager_id" in changes:
        if changes["manager_id"] == target.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A user can't be their own manager.")
        _validate_manager(db, changes["manager_id"])
        target.manager_id = changes["manager_id"]
    if "name" in changes:
        target.name = changes["name"]

    audit.record(db, actor_id=actor.id, action="user.update", target_type="user", target_id=target.id,
                 before=before, after=snapshot(target), request=request)
    db.commit()
    db.refresh(target)
    return target


def _cancel_running_work(user_id: str) -> dict:
    cancelled_tasks = 0
    for task_id in task_ids_for_user(user_id):
        try:
            celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
            cancelled_tasks += 1
        except Exception:  # noqa: BLE001 — a broker hiccup must not block disabling the account
            logger.exception("could not revoke task %s for disabled user %s", task_id, user_id)
    cancelled_runs = pipeline_progress.cancel_runs_for_user(user_id)
    return {"tasks_revoked": cancelled_tasks, "runs_cancelled": cancelled_runs}


def set_active(db: Session, actor: User, target: User, active: bool, request: Request) -> User:
    if target.id == actor.id and not active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't disable your own account.")
    if not active:
        _guard_last_admin(db, target)

    before = snapshot(target)
    target.is_active = active
    after = snapshot(target)
    if not active:
        sessions.revoke_all_sessions(target)
        after["cancelled"] = _cancel_running_work(target.id)

    audit.record(db, actor_id=actor.id, action="user.enable" if active else "user.disable",
                 target_type="user", target_id=target.id, before=before, after=after, request=request)
    db.commit()
    db.refresh(target)
    return target


def reset_sessions(db: Session, actor: User, target: User, request: Request) -> None:
    sessions.revoke_all_sessions(target)
    audit.record(db, actor_id=actor.id, action="user.revoke_sessions", target_type="user",
                 target_id=target.id, request=request)
    db.commit()
