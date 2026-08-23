"""
app/services/browser_apply.py
--------------------------------
Application status is now recorded via app.services.applications_store
.record_status() (DB upsert + history event) instead of a CSV row. The
CSV write is kept as a secondary, best-effort audit trail only — never
the source of truth, and never allowed to block or fail the DB write.
"""
from __future__ import annotations

import csv
import datetime
import time
from pathlib import Path
from typing import Callable
import logging
from playwright.sync_api import sync_playwright

from app.services.applications_store import record_status

logger = logging.getLogger("job_finder_api.apply")

NoOpProgress: Callable[[str], None] = lambda msg: None


def init_log(log_path: Path) -> None:
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["date", "source", "title", "company", "url", "status"])


def _append_csv(log_path: Path, job: dict, status: str) -> None:
    """Best-effort audit trail only — failures here must never block the
    authoritative DB write in log_action()."""
    try:
        with log_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.date.today().isoformat(),
                job.get("source", ""),
                job.get("title", ""),
                job.get("company", ""),
                job.get("url", ""),
                status,
            ])
    except Exception as e:  # noqa: BLE001
        logger.warning("CSV audit-log write failed (non-fatal): %s", e)


def log_action(log_path: Path, job: dict, status: str, *, user_id: str) -> dict:
    """Authoritative write: upserts the applications table + history event.
    Raises if that fails — callers should let it propagate rather than
    catch-and-continue, so a broken tracking write is visible in the
    Celery task's progress/error state instead of disappearing."""
    _append_csv(log_path, job, status)
    row = record_status(user_id=user_id, job=job, status=status, status_source="browser_apply")
    return {
        "id": row.id,
        "date": row.applied_at.date().isoformat(),
        "source": row.source,
        "title": row.job_title,
        "company": row.company,
        "url": row.job_url,
        "status": row.status,
    }


def _try_easy_apply(page, applicant_info: dict) -> str:
    """Opens + partially fills a LinkedIn Easy Apply modal. Never clicks
    final submit — the human reviews and submits themselves."""
    try:
        easy_apply_btn = page.get_by_role("button", name="Easy Apply")
        if easy_apply_btn.count() == 0:
            return "no_easy_apply_button"

        easy_apply_btn.first.click()
        page.wait_for_timeout(1500)

        filled_any = False
        for label, value in [
            ("First name", applicant_info.get("first_name", "")),
            ("Last name", applicant_info.get("last_name", "")),
            ("Email", applicant_info.get("email", "")),
            ("Phone number", applicant_info.get("phone", "")),
        ]:
            if not value:
                continue
            field = page.get_by_label(label)
            if field.count() > 0:
                try:
                    field.first.fill(value)
                    filled_any = True
                except Exception:
                    pass

        return "easy_apply_form_filled_awaiting_review" if filled_any else "easy_apply_opened_no_fields_matched"
    except Exception as e:  # noqa: BLE001
        return f"easy_apply_error:{e}"


def open_and_prepare(
    jobs: list[dict],
    *,
    applicant_info: dict,
    open_top_n: int,
    auto_fill_easy_apply: bool,
    pause_between_tabs_sec: float,
    browser_profile_dir: str,
    applications_log_path: Path,
    user_id: str,
    headless: bool = False,
    progress: Callable[[str], None] = NoOpProgress,
) -> dict:
    """Opens jobs in a persistent browser context. For every job, records
    'opened' as the first status the instant the tab loads, then — for
    LinkedIn jobs with auto-fill on — records the Easy Apply outcome as a
    SEPARATE follow-up status update on the same application row.

    Returns the log entries plus a `handle` dict (playwright instance +
    context) so the caller can keep the browser open for review."""
    init_log(applications_log_path)
    top_jobs = jobs[:open_top_n]
    log_entries: list[dict] = []

    if not top_jobs:
        progress("no jobs to open")
        return {"log": [], "opened_count": 0, "handle": None}

    progress(f"opening top {len(top_jobs)} matches in browser")

    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(browser_profile_dir, headless=headless)

    for job in top_jobs:
        page = context.new_page()
        try:
            page.goto(job["url"], timeout=30000)
            page.wait_for_timeout(1500)

            # Always record "opened" first — guaranteed for every job the
            # instant the tab loads, regardless of what happens next.
            entry = log_action(applications_log_path, job, "opened", user_id=user_id)
            log_entries.append(entry)
            progress(f"[{job.get('llm_score', '?')}] {job['title']} @ {job['company']} ({job['source']}) -> opened")

            if auto_fill_easy_apply and job.get("source") == "LinkedIn":
                fill_status = _try_easy_apply(page, applicant_info)
                fill_entry = log_action(applications_log_path, job, fill_status, user_id=user_id)
                log_entries.append(fill_entry)
                progress(f"[{job.get('llm_score', '?')}] {job['title']} @ {job['company']} ({job['source']}) -> {fill_status}")

        except Exception as e:  # noqa: BLE001
            entry = log_action(applications_log_path, job, f"open_failed:{e}", user_id=user_id)
            log_entries.append(entry)
            progress(f"failed to open {job.get('title')}: {e}")

        time.sleep(pause_between_tabs_sec)

    progress("all tabs opened — review and submit manually, then call the close endpoint")

    return {
        "log": log_entries,
        "opened_count": len(log_entries),
        "handle": {"playwright": playwright, "context": context},
    }


def close_browser(handle: dict) -> None:
    if not handle:
        return
    context = handle.get("context")
    playwright = handle.get("playwright")
    try:
        if context:
            context.close()
    finally:
        if playwright:
            playwright.stop()