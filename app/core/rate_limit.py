"""
app/core/rate_limit.py
------------------------
Rate limiting via slowapi (a lightweight wrapper around the `limits`
library). Applied to the expensive endpoints specifically — /pipeline/run,
/search, /score, /apply — since those are what could burn through your
Gemini/HF/Apify quotas or pin the ThreadPoolExecutor if someone hammers
them. Cheap endpoints (/health, /tasks/{id} polling) are NOT rate-limited
here since polling needs to happen frequently and GET /company-ats/fetch
is already fast/cheap.

Keyed by API key, not IP: per-IP alone under-limits legitimate callers
behind shared NAT/corporate proxies, and is trivially bypassed by anyone
rotating IPs. Since every rate-limited route already requires
X-API-Key (see auth.py), keying on that gives each caller their own
bucket regardless of IP. Falls back to IP only when no key was sent —
i.e. when API_KEYS is empty server-side and auth is open (dev mode).

Usage in a route file:

    from app.core.rate_limit import limiter

    @router.post("/run", dependencies=[Depends(require_api_key)])
    @limiter.limit("5/minute")
    async def run_pipeline(request: Request, req: PipelineRequest):
        ...

NOTE: slowapi requires the route function to accept a `request: Request`
parameter even if unused, because it reads the client IP/header off it.
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logger = logging.getLogger("job_finder_api.ratelimit")


def _rate_limit_key(request: Request) -> str:
    """Per-API-key bucket when a key is present, else per-IP. A caller's
    quota follows their key across IPs/proxies, and one caller can no
    longer starve another's budget by sharing a NAT gateway."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{api_key}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_limit_key)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning("rate limit exceeded: %s %s from %s", request.method, request.url.path,
                    _rate_limit_key(request))
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}. Try again shortly."},
    )