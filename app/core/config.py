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
    # Fallback for Anthropic models when no platform key is stored (P2 key store).
    ANTHROPIC_API_KEY: str = ""

    # Free job-source keys (.env fallback; keys stored via /keys or /admin/keys win).
    ADZUNA_API_KEY: str = ""      # "APP_ID:APP_KEY"
    JOOBLE_API_KEY: str = ""
    CAREERJET_API_KEY: str = ""
    # Google Jobs through the user's own free key (P3.2). .env values are for local testing only:
    # these sources never use a shared key in normal runs.
    BRIGHTDATA_API_KEY: str = ""  # "API_KEY:ZONE"
    SERPAPI_API_KEY: str = ""
    JSEARCH_API_KEY: str = ""
    THEMUSE_API_KEY: str = ""     # platform key for The Muse feed (shared cache)

    # ---- job sources (CLAUDE.md 7.4) ----
    SOURCE_HTTP_TIMEOUT_SECONDS: int = 20
    FEED_CACHE_TTL_SECONDS: int = 3600          # remote feeds downloaded at most once per TTL
    SOURCE_BREAKER_FAILURES: int = 5            # consecutive failures before a source is paused
    SOURCE_BREAKER_COOLDOWN_SECONDS: int = 1800
    SOURCE_DAILY_CAP_OWN_KEY: int = 100         # keyed-API calls per user per day with their own key
    SOURCE_DAILY_CAP_PLATFORM_KEY: int = 20     # ... per user per day on the shared platform key
    SOURCE_DAILY_CAP_PLATFORM_TOTAL: int = 200  # ... all users together per day on the platform key
    HN_FEED_CACHE_TTL_SECONDS: int = 21600      # HN "Who is hiring?" changes slowly
    APPLICANT_COUNT_MAX_PER_RUN: int = 20       # LinkedIn job pages checked per keyword task

    # ---- job alerts (P3.2) ----
    JOB_ALERTS_MAX_PER_USER: int = 3
    JOB_ALERT_TICK_SECONDS: int = 600
    JOB_ALERT_DEFAULT_MIN_SCORE: int = 75

    # ---- AI router (CLAUDE.md 7.9) ----
    AI_BREAKER_FAILURES: int = 3          # consecutive failures before a model is skipped
    AI_BREAKER_COOLDOWN_SECONDS: int = 300
    AI_REQUEST_TIMEOUT_SECONDS: int = 60

    # ---- Celery / Redis (Upstash) ----
    # Get this from your Upstash console: Database -> Details -> "Redis
    # Connect" -> copy the "rediss://..." URL (TLS, note double-s).
    REDIS_URL: str = ""

    # ---- auth ----
    API_KEYS: str = ""  # comma-separated, e.g. "key1,key2"
    # ---- email tracking (Gmail, P5) ----
    GMAIL_CLIENT_ID: str = ""          # Google OAuth *web* client for Gmail (separate consent from sign-in)
    GMAIL_CLIENT_SECRET: str = ""
    GMAIL_REDIRECT_URI: str = "http://127.0.0.1:8000/api/v1/gmail/callback"
    GMAIL_TICK_SECONDS: int = 300               # beat tick: claim accounts that are due
    GMAIL_SYNC_INTERVAL_SECONDS: int = 900      # how often one account is synced
    GMAIL_INITIAL_LOOKBACK_DAYS: int = 30
    GMAIL_MAX_MESSAGES_PER_SYNC: int = 100
    GMAIL_ACCOUNTS_MAX_PER_USER: int = 3
    EMAIL_MATCH_MIN_SCORE: int = 60
    EMAIL_MATCH_MIN_MARGIN: int = 15
    EMAIL_CLASSIFY_MIN_CONFIDENCE: float = 0.7
    EMAIL_EVENT_RETENTION_DAYS: int = 180
    EMAIL_JOBS_RETENTION_DAYS: int = 30
    RATE_LIMIT_GMAIL_SYNC: str = "6/hour"
    # ---- ATS Discovery (P6) ----
    ATS_CRAWL_TICK_SECONDS: int = 300           # beat tick: claim companies that are due
    ATS_CRAWL_BATCH: int = 50                   # companies claimed per tick
    ATS_HOT_REFRESH_HOURS: int = 6              # companies people search / saved jobs
    ATS_COLD_REFRESH_HOURS: int = 24
    ATS_HOST_CONCURRENCY: int = 2               # parallel requests per ATS host
    ATS_MAX_JOBS_PER_COMPANY: int = 1000
    ATS_WORKDAY_MAX_JOBS: int = 400
    ATS_INDEX_COUNTRIES: str = "India,Remote"   # markets indexed first for very large tenants
    ATS_MIN_COMPANIES: int = 10                 # fewer matches -> live top-up
    ATS_TOPUP_MAX_COMPANIES: int = 40
    ATS_TOPUP_SECONDS: int = 20
    ATS_STALE_DAYS: int = 3                     # capped crawls close jobs not seen for this long
    ATS_CLOSED_RETENTION_DAYS: int = 7
    ATS_MAX_INDEXED_JOBS: int = 300000
    ATS_SEARCH_CACHE_SECONDS: int = 600
    ATS_GONE_AFTER: int = 3                     # 404s in a row before a company is switched off
    RATE_LIMIT_ATS_SEARCH: str = "30/minute"
    RATE_LIMIT_ATS_ADD_COMPANY: str = "10/hour"

    @property
    def ats_markets(self) -> tuple[str, ...]:
        return tuple(m.strip() for m in self.ATS_INDEX_COUNTRIES.split(",") if m.strip())

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

    @property
    def maintenance_queue(self) -> str:
        return f"maintenance{self.queue_suffix}"

    @property
    def email_queue(self) -> str:
        return f"email{self.queue_suffix}"

    @property
    def worker_queues(self) -> str:
        """Comma-separated -Q list for a worker that serves every queue
        the API publishes to. start.sh reads this so the worker can never
        drift from the names routes dispatch to (the stuck-`pending` bug)."""
        return f"{self.default_queue},{self.apply_queue},{self.maintenance_queue},{self.email_queue}"

    # ---- signup ----
    # Interim switch until OTP-verified signup (CLAUDE.md 7.1) lands.
    ALLOW_SELF_REGISTRATION: bool = True

    # ---- secret references ----
    # Master key for encrypting user-supplied API keys that tasks resolve
    # by key_ref (see app/services/secret_refs.py). Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    SECRETS_MASTER_KEY: str = ""

    # ---- rate limiting ----
    RATE_LIMIT_PIPELINE: str = "5/minute"
    RATE_LIMIT_SEARCH: str = "10/minute"
    RATE_LIMIT_SCORE: str = "10/minute"
    RATE_LIMIT_APPLY: str = "5/minute"
    RATE_LIMIT_APPLICATIONS: str = "30/minute"
    RATE_LIMIT_RESUME_UPLOAD: str = "10/hour"
    RATE_LIMIT_PROFILE_AI: str = "20/hour"   # resume re-parse, title suggestions

    # ---- profile & search (P3) ----
    RESUME_MAX_BYTES: int = 5 * 1024 * 1024
    RESUME_MAX_PER_USER: int = 20
    RESUME_PARSE_MAX_CHARS: int = 20000      # resume text sent to the parser model
    SEARCH_PROFILES_MAX_PER_USER: int = 20

    # ---- apply center (P4a) ----
    RATE_LIMIT_APPLY_PREP: str = "30/hour"   # prep packs (tailored resume + cover letter)
    ANSWER_BANK_MAX_PER_USER: int = 500
    # ---- apply engine (P4b) ----
    APPLY_DAILY_CAP: int = 20                # auto submits per user per day
    APPLY_PLATFORM_DAILY_CAP: int = 15       # per adapter (greenhouse / lever) per user per day
    FAST_APPLY_DAILY_CAP: int = 25           # LinkedIn Fast apply prefills per user per day
    APPLY_COMPANY_COOLDOWN_DAYS: int = 30    # one application per company + title in this window
    APPLY_MIN_GAP_SECONDS: int = 30          # random gap between two submits on the same platform
    APPLY_MAX_GAP_SECONDS: int = 90
    APPLY_PROOF_RETENTION_DAYS: int = 90
    APPLY_HEADLESS: bool = True              # auto runs; Fast apply always opens a visible browser
    APPLY_PAGE_TIMEOUT_SECONDS: int = 45
    RATE_LIMIT_APPLY_RUNS: str = "30/hour"
    # ---- app / infra ----
    APP_NAME: str = "Job Finder API"
    APP_PUBLIC_URL: str = ""  # frontend URL, used in emails (e.g. alert digests)
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
    # Short-lived access token + rotating refresh cookie (CLAUDE.md 7.1).
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    REFRESH_COOKIE_NAME: str = "jf_refresh"
    REFRESH_COOKIE_SECURE: bool = True
    # "none" because the frontend (Vercel) and API live on different sites.
    REFRESH_COOKIE_SAMESITE: str = "none"

    # ---- sign-in: Google + email OTP ----
    GOOGLE_CLIENT_IDS: str = ""  # comma-separated OAuth client ids accepted as `aud`
    OTP_TTL_SECONDS: int = 600
    OTP_MAX_ATTEMPTS: int = 5
    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    OTP_MAX_REQUESTS_PER_HOUR: int = 5
    RATE_LIMIT_AUTH: str = "10/minute"

    # ---- outgoing email (OTP codes) ----
    # "console" logs the code (development only — refused in production);
    # "smtp" works with Resend, SES, Gmail or any SMTP relay.
    EMAIL_BACKEND: str = "console"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_STARTTLS: bool = True

    @property
    def google_client_ids_list(self) -> list[str]:
        return [x.strip() for x in self.GOOGLE_CLIENT_IDS.split(",") if x.strip()]


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

    if not settings.SECRETS_MASTER_KEY:
        if settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "FATAL: ENVIRONMENT=production but SECRETS_MASTER_KEY is empty. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        logger.warning(
            "startup: SECRETS_MASTER_KEY is empty — user-supplied API keys are "
            "encrypted with a key derived from JWT_SECRET_KEY. Set a dedicated "
            "key before any real use."
        )

    if settings.ALLOW_SELF_REGISTRATION and settings.ENVIRONMENT == "production":
        logger.warning(
            "startup: ALLOW_SELF_REGISTRATION=true in production — anyone can "
            "create an account (no email verification until OTP signup lands)."
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