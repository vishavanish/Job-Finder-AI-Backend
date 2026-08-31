"""
app/core/health.py
--------------------
"""
from __future__ import annotations

from sqlalchemy import text
import redis
from app.core.config import get_settings
from app.core.db import engine

def check_redis() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.REDIS_URL:
        return False, "REDIS_URL is not set"
 
    try:
        client = redis.from_url(settings.REDIS_URL, socket_timeout=5)
        client.ping()
        queue_len = client.llen("celery")
        return True, f"reachable, {queue_len} task(s) queued on 'celery'"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    
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
    redis_ok, redis_detail = check_redis()
    llm_ok, llm_detail = check_llm_config()
 
    checks = {
        "database": {"ok": db_ok, "detail": db_detail},
        "redis": {"ok": redis_ok, "detail": redis_detail},
        "llm_config": {"ok": llm_ok, "detail": llm_detail},
    }
 
    if not db_ok or not redis_ok:
        overall = "unhealthy"
    elif not llm_ok:
        overall = "degraded"
    else:
        overall = "ok"
 
    return {"status": overall, "app": settings.APP_NAME, "checks": checks}
 