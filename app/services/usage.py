"""
app/services/usage.py
-----------------------
Summaries over usage_ledger (CLAUDE.md 7.9): totals, per model, per day and
(for admins) per user. Aggregation runs in SQL so it stays cheap as the
ledger grows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, case, cast, func
from sqlalchemy.orm import Session

from app.models.ai import UsageLedger
from app.models.user import User


def _totals_cols():
    failed = func.sum(case((UsageLedger.success.is_(False), 1), else_=0))
    return (
        func.count(UsageLedger.id).label("calls"),
        cast(func.coalesce(failed, 0), Integer).label("failed_calls"),
        cast(func.coalesce(func.sum(UsageLedger.tokens_in), 0), Integer).label("tokens_in"),
        cast(func.coalesce(func.sum(UsageLedger.tokens_out), 0), Integer).label("tokens_out"),
        func.coalesce(func.sum(UsageLedger.cost_usd), 0.0).label("cost_usd"),
    )


def _totals(row) -> dict:
    return {"calls": int(row.calls or 0), "failed_calls": int(row.failed_calls or 0),
            "tokens_in": int(row.tokens_in or 0), "tokens_out": int(row.tokens_out or 0),
            "cost_usd": round(float(row.cost_usd or 0), 6)}


def summarize(db: Session, *, user_id: str | None = None, days: int = 30, by_user: bool = False) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = [UsageLedger.created_at >= since]
    if user_id is not None:
        base.append(UsageLedger.user_id == user_id)

    totals = db.query(*_totals_cols()).filter(*base).one()

    model_rows = (db.query(UsageLedger.model_ref, UsageLedger.provider, UsageLedger.model_id, *_totals_cols())
                  .filter(*base)
                  .group_by(UsageLedger.model_ref, UsageLedger.provider, UsageLedger.model_id)
                  .order_by(func.count(UsageLedger.id).desc()).all())

    day = func.date(UsageLedger.created_at)
    day_rows = db.query(day.label("day"), *_totals_cols()).filter(*base).group_by(day).order_by(day).all()

    result = {
        "since": since,
        "totals": _totals(totals),
        "by_model": [{"model_ref": r.model_ref, "provider": r.provider, "model_id": r.model_id, **_totals(r)}
                     for r in model_rows],
        "by_day": [{"day": str(r.day), **_totals(r)} for r in day_rows],
    }
    if by_user:
        user_rows = (db.query(UsageLedger.user_id, User.email, *_totals_cols())
                     .outerjoin(User, User.id == UsageLedger.user_id)
                     .filter(*base)
                     .group_by(UsageLedger.user_id, User.email)
                     .order_by(func.coalesce(func.sum(UsageLedger.cost_usd), 0.0).desc())
                     .limit(100).all())
        result["by_user"] = [{"user_id": r.user_id, "email": r.email, **_totals(r)} for r in user_rows]
    return result
