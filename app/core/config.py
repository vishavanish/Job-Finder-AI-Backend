"""
app/core/config.py
-------------------
ONLY server-side secrets and infra paths live here (env-driven).
"""
import logging
import re
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger("job_finder_api")


class SecretRedactionFilter(logging.Filter):
    _PATTERNS = [
        re.compile(r'(api_key["\']?\s*[:=]\s*["\']?)[\w\-]{8,}', re.IGNORECASE),
        re.compile(r'(token["\']?\s*[:=]\s*["\']?)[\w\-]{8,}', re.IGNORECASE),
        re.compile(r'(Bearer\s+)[\w\-\.]{8,}', re.IGNORECASE),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern in self._PATTERNS:
            msg = pattern.sub(r'\1***REDACTED***', msg)
        record.msg = msg
        record.args = ()
        return True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    # ---- deployment mode ----
    ENVIRONMENT: str = "development"  # "development" | "production"

    # ---- provider secrets ----
    HF_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    APIFY_API_TOKEN: str = ""

    # ---- Celery / Redis (Upstash) ----
    # Get this from your Upstash console: Database -> Details -> "Redis
    # Connect" -> copy the "rediss://..." URL (TLS, note double-s).
    REDIS_URL: str = ""

    # ---- auth ----
    API_KEYS: str = ""  # comma-separated, e.g. "key1,key2"
    # ---- email tracking (Gmail) ----
    GMAIL_ACCOUNT_LABELS: str = "primary,secondary"  # comma-separated, matches token_<label>.json files
    GMAIL_CREDENTIALS_PATH: Path = BASE_DIR / "credentials.json"
    GMAIL_POLL_LOOKBACK_MINUTES: int = 30  # slightly > beat interval, to survive one missed run

    @property
    def gmail_account_labels_list(self) -> list[str]:
        return [x.strip() for x in self.GMAIL_ACCOUNT_LABELS.split(",") if x.strip()]

    @property
    def api_keys_list(self) -> list[str]:
        return [k.strip() for k in self.API_KEYS.split(",") if k.strip()]

    @property
    def queue_suffix(self) -> str:
        """Appended to every queue name so local dev and the Oracle prod
        worker never compete for the same messages on the same Redis
        broker. development -> "_dev" suffix; production -> no suffix
        (keeps existing prod queue names "default"/"apply" unchanged, so
        Oracle needs zero changes)."""
        return "" if self.ENVIRONMENT == "production" else "_dev"

    @property
    def default_queue(self) -> str:
        return f"default{self.queue_suffix}"

    @property
    def apply_queue(self) -> str:
        return f"apply{self.queue_suffix}"
    
    # ---- rate limiting ----
    RATE_LIMIT_PIPELINE: str = "5/minute"
    RATE_LIMIT_SEARCH: str = "10/minute"
    RATE_LIMIT_SCORE: str = "10/minute"
    RATE_LIMIT_APPLY: str = "5/minute"
    RATE_LIMIT_APPLICATIONS: str = "30/minute"
    # ---- app / infra ----
    APP_NAME: str = "Job Finder API"
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = ["*"]

    # ---- storage ----
    OUTPUT_DIR: Path = BASE_DIR / "output"
    BROWSER_PROFILE_DIR: Path = BASE_DIR / "browser_profile"
    LOG_DIR: Path = BASE_DIR / "logs"

    # ---- database (Supabase Postgres) ----
    
    DATABASE_URL: str = ""
    DIRECT_URL: str = ""
    # Deprecated: kept only so old .env files with AUTH_DATABASE_URL set
    # don't silently do nothing. If set, it's used as DATABASE_URL when
    # DATABASE_URL itself is empty. New setups should set DATABASE_URL only.
    AUTH_DATABASE_URL: str = ""

    MAX_WORKER_THREADS: int = 4
    TASK_RESULT_TTL_SECONDS: int = 60 * 60 * 6  # 6h

    JWT_SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days


@lru_cache
def get_settings() -> Settings:
    settings = Settings()

    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)

    env_path = BASE_DIR / ".env"
    logger.info("startup: env_file=%s exists=%s", env_path, env_path.exists())
    logger.info("startup: ENVIRONMENT=%s", settings.ENVIRONMENT)

    if not settings.REDIS_URL:
        logger.warning(
            "startup: REDIS_URL is empty — Celery has no broker/backend "
            "configured. Background tasks (pipeline/search/score/apply) "
            "will fail to dispatch. Set REDIS_URL from your Upstash "
            "console in .env."
        )
        
    elif not settings.REDIS_URL.startswith("rediss://"):
        logger.warning(
            "startup: REDIS_URL does not start with 'rediss://' (TLS) — "
            "Upstash requires TLS on its free tier; a plain 'redis://' "
            "URL will likely fail to connect."
        )

    # Fold the deprecated AUTH_DATABASE_URL into DATABASE_URL if that's all
    # an old .env file has set, so upgrading doesn't silently break auth.
    if not settings.DATABASE_URL and settings.AUTH_DATABASE_URL:
        logger.warning(
            "startup: DATABASE_URL is empty but AUTH_DATABASE_URL is set — "
            "using it as DATABASE_URL. AUTH_DATABASE_URL is deprecated; "
            "rename it to DATABASE_URL in your .env."
        )
        settings.DATABASE_URL = settings.AUTH_DATABASE_URL

    if not settings.DATABASE_URL:
        if settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "FATAL: ENVIRONMENT=production but DATABASE_URL is empty. "
                "Set it to your Supabase Postgres connection string (Project "
                "Settings -> Database -> Connection string -> pooler, "
                "transaction mode, port 6543)."
            )
        logger.warning(
            "startup: DATABASE_URL is empty — the app has no database "
            "configured. Set it to your Supabase Postgres connection string "
            "in .env before starting the API or a Celery worker."
        )
    elif not settings.DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg2://", "postgresql+psycopg://")):
        logger.warning(
            "startup: DATABASE_URL does not look like a Postgres URL "
            "(expected it to start with 'postgresql://'). If you copy-pasted "
            "Supabase's string verbatim it may start with 'postgres://' — "
            "SQLAlchemy needs 'postgresql://', so update the scheme."
        )
    elif ".pooler.supabase.com" not in settings.DATABASE_URL and "supabase.co" in settings.DATABASE_URL:
        logger.warning(
            "startup: DATABASE_URL points at Supabase's DIRECT connection "
            "host, not the pooler. That's fine for a single long-lived "
            "process, but Render + multiple Celery workers will burn "
            "through Supabase free tier's ~60 direct-connection limit fast. "
            "Prefer the pooler (transaction mode, port 6543) connection "
            "string instead."
        )

    missing = [name for name in ("HF_API_KEY", "GEMINI_API_KEY") if not getattr(settings, name)]
    if len(missing) == 2:
        logger.warning(
            "startup: NEITHER HF_API_KEY nor GEMINI_API_KEY is set — any "
            "/score or /pipeline call that doesn't supply its own key "
            "will fail at the scoring step."
        )
    if not settings.JWT_SECRET_KEY:
        if settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "FATAL: ENVIRONMENT=production but JWT_SECRET_KEY is empty. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        logger.warning(
            "startup: JWT_SECRET_KEY is empty — set this in .env before any real use "
            "(tokens cannot be signed/verified without it)."
        )

    if not settings.api_keys_list:
        if settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "FATAL: ENVIRONMENT=production but API_KEYS is empty. "
                "Refusing to start with no authentication. Set at least "
                "one key in API_KEYS, or set ENVIRONMENT=development if "
                "this is intentional for local testing."
            )
        logger.warning(
            "startup: API_KEYS is empty — this API has NO AUTHENTICATION."
        )
    else:
        logger.info("startup: auth enabled with %d configured API key(s)", len(settings.api_keys_list))

    return settings