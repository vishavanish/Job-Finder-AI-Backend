"""
app/core/health.py
--------------------
A real health check, not a static {"status": "ok"}. Verifies the things
that actually need to work for this API to function: the Postgres
(Supabase) database is reachable, and at least one LLM provider key is
configured somewhere reachable (server-side or expected to be supplied
per-request).

Returns HTTP 200 with "ok" or "degraded", or 503 with "unhealthy" — a
load balancer / uptime monitor can act on the status code directly
without parsing the body.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import engine


def check_database() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "reachable"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"


def check_llm_config() -> tuple[bool, str]:
    settings = get_settings()
    have_gemini = bool(settings.GEMINI_API_KEY)
    have_hf = bool(settings.HF_API_KEY)
    if have_gemini or have_hf:
        return True, f"gemini={have_gemini} hf={have_hf}"
    return False, "no server-side LLM key configured (requests must supply their own)"


def run_health_check() -> dict:
    settings = get_settings()

    db_ok, db_detail = check_database()
    llm_ok, llm_detail = check_llm_config()

    checks = {
        "database": {"ok": db_ok, "detail": db_detail},
        "llm_config": {"ok": llm_ok, "detail": llm_detail},
    }

    if not db_ok:
        overall = "unhealthy"
    elif not llm_ok:
        overall = "degraded"
    else:
        overall = "ok"

    return {"status": overall, "app": settings.APP_NAME, "checks": checks}