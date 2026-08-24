"""
app/main.py
------------
FastAPI entrypoint. Run with:
    uvicorn app.main:app --reload --port 8000

Interactive docs at /docs (Swagger) and /redoc once running.
"""
import logging
import time
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.api.routes import api_router
from app.core.config import get_settings
from app.core.health import run_health_check
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.core.db import Base, engine
from app.models import user , application

settings = get_settings()  # also runs startup config validation (logs warnings)
Base.metadata.create_all(bind=engine)
# ---- logging: console + rotating file, so logs survive beyond terminal
# scrollback and don't grow unbounded on disk ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        RotatingFileHandler(
            settings.LOG_DIR / "job_finder.log",
            maxBytes=5_000_000,   # 5MB per file
            backupCount=5,        # keep last 5 rotated files (~25MB total)
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("job_finder_api")

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "Job search / scoring / auto-apply pipeline as an API. Every field "
        "that used to live in a hardcoded config.py (keywords, locations, "
        "skills, resume, applicant info, ATS targets, thresholds) is now "
        "part of the request body, so a frontend can drive the whole "
        "pipeline dynamically."
    ),
)

# ---- rate limiting ----
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://job-finder-ai-frontend-one.vercel.app"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    logger.info("%s %s -> %s (%.3fs)", request.method, request.url.path, response.status_code, duration)
    return response




@app.get("/health", tags=["meta"])
async def health():
    result = run_health_check()
    if result["status"] == "unhealthy":
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=result)
    return result