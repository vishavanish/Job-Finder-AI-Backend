"""
app/core/health.py
--------------------
A real health check, not a static {"status": "ok"}. Verifies the things
that actually need to work for this API to function: the SQLite task
store is writable, and at least one LLM provider key is configured
somewhere reachable (server-side or expected to be supplied per-request).

Returns HTTP 200 with "ok" or "degraded", or 503 with "unhealthy" — a
load balancer / uptime monitor can act on the status code directly
without parsing the body.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from app.core.config import get_settings


def check_database(db_path_str: str) -> tuple[bool, str]:
    try:
        path_str = db_path_str.split("sqlite:///", 1)[-1]
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("CREATE TABLE IF NOT EXISTS _health_check (id TEXT)")
        test_id = str(uuid.uuid4())
        conn.execute("INSERT INTO _health_check (id) VALUES (?)", (test_id,))
        conn.execute("DELETE FROM _health_check WHERE id = ?", (test_id,))
        conn.commit()
        conn.close()
        return True, "writable"
    except Exception as e:  # noqa: BLE001
        return False, f"not writable: {e}"


def check_llm_config() -> tuple[bool, str]:
    settings = get_settings()
    have_gemini = bool(settings.GEMINI_API_KEY)
    have_hf = bool(settings.HF_API_KEY)
    if have_gemini or have_hf:
        return True, f"gemini={have_gemini} hf={have_hf}"
    # Not necessarily fatal — callers can supply their own key per-request —
    # but worth surfacing as degraded rather than silently "ok".
    return False, "no server-side LLM key configured (requests must supply their own)"


def run_health_check() -> dict:
    settings = get_settings()

    db_ok, db_detail = check_database(settings.DATABASE_URL)
    llm_ok, llm_detail = check_llm_config()

    checks = {
        "database": {"ok": db_ok, "detail": db_detail},
        "llm_config": {"ok": llm_ok, "detail": llm_detail},
    }

    if not db_ok:
        overall = "unhealthy"  # can't persist tasks at all -> real outage
    elif not llm_ok:
        overall = "degraded"   # scoring will fail unless requests bring their own key
    else:
        overall = "ok"

    return {"status": overall, "app": settings.APP_NAME, "checks": checks}