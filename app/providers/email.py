"""
app/providers/email.py
-------------------------
Outgoing transactional email behind one interface (CLAUDE.md 0.4, 7.1).
EMAIL_BACKEND picks the implementation:

  - "console": logs the message. Development only — get_email_sender()
    refuses it in production so OTP codes never land in prod logs.
  - "smtp": any SMTP relay (Resend, Amazon SES, Gmail, Mailgun...).

`html` (optional) is sent as the text/html alternative of the same message; `body` is always the
text/plain part, so clients without HTML still get the full email.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger("job_finder_api.email")


class EmailNotConfigured(RuntimeError):
    pass


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str, html: str | None = None) -> None: ...


class ConsoleEmailSender:
    def send(self, *, to: str, subject: str, body: str, html: str | None = None) -> None:
        logger.info("[console email] to=%s subject=%r%s\n%s", to, subject,
                    f" (+ {len(html)} chars of HTML)" if html else "", body)


class SmtpEmailSender:
    def send(self, *, to: str, subject: str, body: str, html: str | None = None) -> None:
        settings = get_settings()
        msg = EmailMessage()
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(msg)


def get_email_sender() -> EmailSender:
    settings = get_settings()
    backend = settings.EMAIL_BACKEND.lower()
    if backend == "smtp":
        if not (settings.SMTP_HOST and settings.SMTP_FROM):
            raise EmailNotConfigured("EMAIL_BACKEND=smtp needs SMTP_HOST and SMTP_FROM.")
        return SmtpEmailSender()
    if backend == "console":
        if settings.ENVIRONMENT == "production":
            raise EmailNotConfigured("EMAIL_BACKEND=console is not allowed in production; configure SMTP.")
        return ConsoleEmailSender()
    raise EmailNotConfigured(f"Unknown EMAIL_BACKEND {settings.EMAIL_BACKEND!r}.")
