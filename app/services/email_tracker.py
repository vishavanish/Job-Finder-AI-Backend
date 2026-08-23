"""
app/services/email_tracker.py
--------------------------------
Polls one or more Gmail accounts (see app.core.config.GMAIL_ACCOUNT_LABELS)
for recent mail, cheaply pre-filters candidates that look like an ATS/
recruiter reply, and hands survivors off for LLM classification (next
step) before updating app.models.application.Application via
app.services.applications_store.record_status().

MULTI-ACCOUNT DESIGN: you may apply from one Gmail address but receive
replies in a different one (forwarding, aliasing, or just checking a
different inbox habitually). Rather than guess which account a reply
lands in, this polls every account listed in GMAIL_ACCOUNT_LABELS and
merges results — a rejection landing in either account is caught.

PRE-FILTER, NOT CLASSIFIER: keyword pre-filtering here is deliberately
loose/high-recall (better to send a few false positives to the LLM step
than silently miss a real rejection). The actual reject/interview/other
decision is made by the LLM in the next step, never by keyword matching
alone.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import get_settings

logger = logging.getLogger("job_finder_api.email_tracker")
NoOpProgress: Callable[[str], None] = lambda msg: None

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# High-recall keyword pre-filter — cheap to run on every fetched message
# before anything reaches the LLM. Intentionally broad; the LLM step
# narrows this down to an actual classification.
_SUBJECT_BODY_HINTS = (
    "application", "applied", "interview", "candidacy", "candidate",
    "position", "role", "recruiting", "recruitment", "hiring", "offer",
    "unfortunately", "regret", "not moving forward", "other candidates",
    "assessment", "online assessment", "next steps", "screening",
    "thank you for your interest", "thank you for applying",
)

# Sender domains that are almost never personal — if the sender's domain
# matches an ATS platform, treat as high-confidence regardless of subject.
_ATS_SENDER_DOMAINS = (
    "greenhouse.io", "lever.co", "smartrecruiters.com", "myworkdayjobs.com",
    "successfactors.com", "icims.com", "taleo.net", "linkedin.com",
    "indeed.com", "naukri.com", "workday.com",
)


@dataclass
class EmailCandidate:
    account_label: str
    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    body_text: str
    received_at: datetime


def _load_credentials(account_label: str) -> Credentials | None:
    settings = get_settings()
    token_path = settings.GMAIL_CREDENTIALS_PATH.parent / f"token_{account_label}.json"

    if not token_path.exists():
        logger.error(
            "no token file for account '%s' at %s — run "
            "`python -m app.scripts.gmail_authorize %s` first",
            account_label, token_path, account_label,
        )
        return None

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        token_path.write_text(creds.to_json())  # persist the refreshed token

    return creds


def _extract_body_text(payload: dict) -> str:
    """Gmail messages can be multipart; walk parts looking for text/plain,
    falling back to text/html (stripped) if that's all that's available."""
    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])

    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode(part["body"]["data"])

    # fall back to html, stripped
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            import re
            html = _decode(part["body"]["data"])
            return re.sub(r"<[^>]+>", " ", html)

    return ""


def _passes_prefilter(sender: str, subject: str, body_text: str) -> bool:
    sender_lower = sender.lower()
    if any(domain in sender_lower for domain in _ATS_SENDER_DOMAINS):
        return True

    haystack = f"{subject} {body_text[:500]}".lower()
    return any(hint in haystack for hint in _SUBJECT_BODY_HINTS)


def fetch_candidates_for_account(
    account_label: str,
    *,
    lookback_minutes: int,
    max_results: int = 50,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[EmailCandidate]:
    creds = _load_credentials(account_label)
    if not creds:
        progress(f"[{account_label}] no valid credentials — skipping this account")
        return []

    service = build("gmail", "v1", credentials=creds)
    after_ts = int((datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).timestamp())

    try:
        resp = service.users().messages().list(
            userId="me",
            q=f"after:{after_ts} category:primary",
            maxResults=max_results,
        ).execute()
    except HttpError as e:
        progress(f"[{account_label}] Gmail list() failed: {e}")
        return []

    message_ids = [m["id"] for m in resp.get("messages", [])]
    if not message_ids:
        progress(f"[{account_label}] no new messages in the last {lookback_minutes}m")
        return []

    candidates: list[EmailCandidate] = []
    for msg_id in message_ids:
        try:
            msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except HttpError as e:
            progress(f"[{account_label}] failed to fetch message {msg_id}: {e}")
            continue

        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        body_text = _extract_body_text(msg["payload"])
        snippet = msg.get("snippet", "")

        if not _passes_prefilter(sender, subject, body_text):
            continue

        candidates.append(EmailCandidate(
            account_label=account_label,
            message_id=msg_id,
            thread_id=msg.get("threadId", ""),
            sender=sender,
            subject=subject,
            snippet=snippet,
            body_text=body_text,
            received_at=datetime.now(timezone.utc),
        ))

    progress(f"[{account_label}] {len(candidates)}/{len(message_ids)} messages passed pre-filter")
    return candidates


def fetch_all_candidates(
    *,
    lookback_minutes: int | None = None,
    progress: Callable[[str], None] = NoOpProgress,
) -> list[EmailCandidate]:
    """Polls every configured Gmail account and merges pre-filtered
    candidates. This is the single entry point the Celery beat task
    calls — it doesn't need to know how many accounts are configured."""
    settings = get_settings()
    lookback = lookback_minutes or settings.GMAIL_POLL_LOOKBACK_MINUTES

    all_candidates: list[EmailCandidate] = []
    for label in settings.gmail_account_labels_list:
        all_candidates.extend(
            fetch_candidates_for_account(label, lookback_minutes=lookback, progress=progress)
        )

    progress(f"total pre-filtered candidates across {len(settings.gmail_account_labels_list)} account(s): {len(all_candidates)}")
    return all_candidates