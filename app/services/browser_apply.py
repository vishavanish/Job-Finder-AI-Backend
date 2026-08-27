"""
app/services/browser_apply.py
--------------------------------
Application status is recorded via app.services.applications_store
.record_status() (DB upsert + history event) instead of a CSV row. The
CSV write is kept as a secondary, best-effort audit trail only — never
the source of truth, and never allowed to block or fail the DB write.

AUTOPILOT CAPABILITY FILTER (new)
----------------------------------
This module used to open a browser tab for every top-ranked job, then
*only* attempt Easy Apply auto-fill if source == "LinkedIn" — everything
else just got a tab opened and logged as "opened" with nothing actually
auto-applied. That's not autopilot, that's tab-spam.

Now: before any tab is opened, jobs are filtered down to
AUTO_APPLY_CAPABLE_SOURCES. Jobs from unsupported sources are skipped
entirely (never opened, never counted as an "application") and reported
back separately so the caller knows why the count is lower than
open_top_n.

AUTO_APPLY_CAPABLE_SOURCES is deliberately a module-level constant, not
a hardcoded string check buried in the loop, so adding a new capability
(e.g. a native Greenhouse/Lever form-filler) later is a one-line change
here plus a new _try_<platform>_apply() function — nothing else in this
file, celery_tasks.py, or the routes needs to change.

"opened_count" (tabs opened) and "auto_applied_count" (Easy Apply forms
genuinely filled) are now reported separately — a LinkedIn job can still
lack an Easy Apply button, so "we opened it" and "we auto-applied to it"
are not the same claim and shouldn't be conflated in the response.
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

# Sources this module can genuinely auto-fill an application form for.
# Today that's LinkedIn Easy Apply only. Extend this set (and add a
# corresponding _try_<platform>_apply() + dispatch entry below) as more
# native fill flows are implemented. Do NOT add a source here just
# because jobs from it can be *opened* — only add it once there's a real
# fill implementation, or the capability filter becomes meaningless.
AUTO_APPLY_CAPABLE_SOURCES = frozenset({"LinkedIn"})

# Statuses that count as a genuine auto-apply (form actually filled),
# as opposed to "we opened the tab but nothing was filled."
_AUTO_APPLIED_STATUSES = frozenset({"easy_apply_form_filled_awaiting_review"})


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


def partition_by_auto_apply_capability(
    jobs: list[dict],
    *,
    progress: Callable[[str], None] = NoOpProgress,
) -> tuple[list[dict], list[dict]]:
    """Splits jobs into (capable, skipped). `capable` are jobs whose
    source has a real auto-fill implementation (AUTO_APPLY_CAPABLE_SOURCES);
    `skipped` are everything else, each annotated with a `skip_reason` so
    the caller can show the user *why* a job wasn't touched rather than
    it silently vanishing.

    This runs BEFORE open_top_n slicing and BEFORE any browser tab is
    opened — a job that can't be auto-applied to shouldn't burn a slot in
    "top N" or a tab in the browser."""
    capable, skipped = [], []
    for job in jobs:
        source = job.get("source", "")
        if source in AUTO_APPLY_CAPABLE_SOURCES:
            capable.append(job)
        else:
            skipped.append({**job, "skip_reason": f"auto-apply not yet supported for source '{source}'"})

    if skipped:
        by_source: dict[str, int] = {}
        for j in skipped:
            by_source[j.get("source", "")] = by_source.get(j.get("source", ""), 0) + 1
        progress(
            f"skipped {len(skipped)} job(s) with no auto-apply support "
            f"({dict(by_source)}) — autopilot only supports {sorted(AUTO_APPLY_CAPABLE_SOURCES)} today"
        )

    return capable, skipped


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


# Dispatch table: source -> fill function. Add entries here as new
# platforms get real auto-fill support (keep AUTO_APPLY_CAPABLE_SOURCES
# in sync with the keys of this dict).
_FILL_DISPATCH: dict[str, Callable] = {
    "LinkedIn": _try_easy_apply,
}


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
    require_auto_apply_capable: bool = True,
    progress: Callable[[str], None] = NoOpProgress,
) -> dict:
    """Opens jobs in a persistent browser context and auto-fills them.

    CAPABILITY FILTERING: when require_auto_apply_capable=True (the
    default — this IS the autopilot flow), jobs are filtered down to
    AUTO_APPLY_CAPABLE_SOURCES *before* open_top_n slicing and *before*
    any tab is opened. Jobs from unsupported sources never get a tab and
    are returned separately under "skipped_jobs" with a reason, not
    silently dropped.

    Set require_auto_apply_capable=False to fall back to the old
    behaviour (open every top-N job for manual human review, regardless
    of auto-fill support) — useful if you want a "open my top matches so
    I can apply by hand" mode distinct from autopilot.

    For every job that IS opened, records 'opened' as the first status
    the instant the tab loads, then — for jobs on a source with a real
    fill implementation, with auto-fill on — records the fill outcome as
    a SEPARATE follow-up status update on the same application row.

    Returns the log entries plus a `handle` dict (playwright instance +
    context) so the caller can keep the browser open for review."""
    init_log(applications_log_path)

    if require_auto_apply_capable:
        capable_jobs, skipped_jobs = partition_by_auto_apply_capability(jobs, progress=progress)
    else:
        capable_jobs, skipped_jobs = jobs, []

    top_jobs = capable_jobs[:open_top_n]
    log_entries: list[dict] = []
    auto_applied_count = 0

    if not top_jobs:
        progress("no auto-apply-capable jobs to open" if require_auto_apply_capable else "no jobs to open")
        return {
            "log": [],
            "opened_count": 0,
            "auto_applied_count": 0,
            "skipped_jobs": skipped_jobs,
            "skipped_count": len(skipped_jobs),
            "handle": None,
        }

    progress(f"opening top {len(top_jobs)} auto-applyable matches in browser")

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

            fill_fn = _FILL_DISPATCH.get(job.get("source", ""))
            if auto_fill_easy_apply and fill_fn:
                fill_status = fill_fn(page, applicant_info)
                fill_entry = log_action(applications_log_path, job, fill_status, user_id=user_id)
                log_entries.append(fill_entry)
                if fill_status in _AUTO_APPLIED_STATUSES:
                    auto_applied_count += 1
                progress(f"[{job.get('llm_score', '?')}] {job['title']} @ {job['company']} ({job['source']}) -> {fill_status}")

        except Exception as e:  # noqa: BLE001
            entry = log_action(applications_log_path, job, f"open_failed:{e}", user_id=user_id)
            log_entries.append(entry)
            progress(f"failed to open {job.get('title')}: {e}")

        time.sleep(pause_between_tabs_sec)

    progress(
        f"all tabs opened — {auto_applied_count}/{len(top_jobs)} genuinely auto-applied "
        f"(rest need manual review) — review and submit manually, then call the close endpoint"
    )

    return {
        "log": log_entries,
        "opened_count": len(top_jobs),
        "auto_applied_count": auto_applied_count,
        "skipped_jobs": skipped_jobs,
        "skipped_count": len(skipped_jobs),
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