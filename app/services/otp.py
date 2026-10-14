"""
app/services/otp.py
----------------------
Email one-time codes (CLAUDE.md 7.1). Rules enforced here:

  - 6 digits from a CSPRNG (secrets.randbelow).
  - Redis stores only an HMAC of (email, code) — never the code — with a
    TTL (OTP_TTL_SECONDS, 10 min). A code is single-use: deleted on success.
  - Max OTP_MAX_ATTEMPTS wrong guesses per code, then it is invalidated.
  - Resend cooldown (OTP_RESEND_COOLDOWN_SECONDS) and an hourly cap per
    email (OTP_MAX_REQUESTS_PER_HOUR); the route adds a per-IP limit.
  - Constant-time comparison (hmac.compare_digest).

Redis keys use a hash of the email, not the address itself.
Whether a code is actually emailed (e.g. not for an uninvited address in
invite-only mode) is the route's decision; the counters advance the same
way either way, so responses don't reveal whether an account exists.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from app.core.config import get_settings
from app.core.redis_client import get_redis


class OtpCooldown(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class OtpTooManyRequests(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _email_id(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:32]


def _hmac_key() -> bytes:
    return hashlib.sha256(b"otp:" + get_settings().JWT_SECRET_KEY.encode()).digest()


def _code_mac(email: str, code: str) -> str:
    message = f"{normalize_email(email)}:{code}".encode()
    return hmac.new(_hmac_key(), message, hashlib.sha256).hexdigest()


def issue_code(email: str) -> str:
    """Creates a new code for `email` (replacing any earlier one) and
    returns it for delivery. Raises OtpCooldown / OtpTooManyRequests."""
    settings = get_settings()
    r = get_redis()
    eid = _email_id(email)

    cooldown_key = f"otp_cooldown:{eid}"
    ttl = r.ttl(cooldown_key)
    if ttl and ttl > 0:
        raise OtpCooldown(retry_after=ttl)

    count_key = f"otp_count:{eid}"
    count = r.incr(count_key)
    if count == 1:
        r.expire(count_key, 3600)
    if count > settings.OTP_MAX_REQUESTS_PER_HOUR:
        raise OtpTooManyRequests()

    code = f"{secrets.randbelow(10**6):06d}"
    code_key = f"otp:{eid}"
    r.delete(code_key)
    r.hset(code_key, mapping={"mac": _code_mac(email, code), "attempts": 0})
    r.expire(code_key, settings.OTP_TTL_SECONDS)
    r.set(cooldown_key, 1, ex=settings.OTP_RESEND_COOLDOWN_SECONDS)
    return code


def verify_code(email: str, code: str) -> bool:
    """True exactly once for the right code within its lifetime."""
    settings = get_settings()
    r = get_redis()
    code_key = f"otp:{_email_id(email)}"

    stored_mac = r.hget(code_key, "mac")
    if not stored_mac:
        return False

    attempts = r.hincrby(code_key, "attempts", 1)
    if attempts > settings.OTP_MAX_ATTEMPTS:
        r.delete(code_key)
        return False

    if hmac.compare_digest(stored_mac, _code_mac(email, code.strip())):
        r.delete(code_key)
        return True

    if attempts >= settings.OTP_MAX_ATTEMPTS:
        r.delete(code_key)
    return False
