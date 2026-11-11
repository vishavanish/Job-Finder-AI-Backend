"""
app/live_browser — QA11 A: the live browser service (`job_finder_browser`).

A small FastAPI app on 127.0.0.1 that owns the live Chromium sessions for
"Sign in to continue": the API asks it to open a run's stop page, the user
sees the page as a stream of JPEG frames (CDP screencast) and clicks/types
through the API's WebSocket, and when they are signed in the API takes the
browser's cookies (storage state) for the apply engine. Kept out of the API
process and out of Celery worker slots because a live Chromium is heavy and
long-lived.

Run:  uvicorn app.live_browser.service:app --host 127.0.0.1 --port 8790
"""
from __future__ import annotations

import hashlib
import hmac

from app.core.config import get_settings


def internal_key() -> str:
    """Shared secret between the API and this service (both read the same .env)."""
    secret = get_settings().JWT_SECRET_KEY.encode()
    return hmac.new(secret, b"job_finder_live_browser", hashlib.sha256).hexdigest()
