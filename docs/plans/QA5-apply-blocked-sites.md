# QA5 — Auto apply on sites that block the server (2026-10-26)

Source: the owner's first live Auto apply run (Sia, SmartRecruiters). The browser reached
`jobs.smartrecruiters.com/oneclick-ui/...` and got SmartRecruiters' block page: "Access is
temporarily restricted … Automated (bot) activity on your network (IP 129.151.47.136)". The run
stopped (correct) but said "No application form was found on the page", and the review screen said
"your answers are below to copy" with no answers. Clicking Auto apply again would hit the same wall.

Branch `dev` in both repos; nothing pushed or deployed without the owner asking.
**Status:** approved 2026-10-26 with the four recommended defaults; built (CLAUDE.md §6.19). The admin list
sits on Admin → Feature defaults (the frontend has no system-health page).
Rule kept: a block is never worked around (no proxies, no user-agent tricks, no retries to get past it).

---

## 1. Detect block pages (backend)

- `forms.blocked(page, response) -> str | None` (`app/apply/forms.py`): the page is a bot/access block
  when
  - the main document answered **403 or 429**, or
  - the body text matches a block pattern: "access is temporarily restricted", "unusual activity
    from your (device|network)", "automated (bot) activity", "you have been blocked",
    "access denied" + "reference #" (Akamai), "attention required" + "cloudflare",
    "request (was )?blocked", "too many requests".
  Kept apart from `challenge_visible` (a captcha a person can solve) — a block page has nothing to solve.
- New `Blocked(HardStop)` in `adapters.py` with `platform` (display name).
- Checked in `Adapter.open` (Greenhouse, Lever) and `GenericAdapter._page_checks` — i.e. right after
  the first page loads **and** after the Apply click, which is where SmartRecruiters blocked.
  `open` keeps the `page.goto` response to read its status.
- Engine (`apply_engine.prepare`, `_prepare_steps`, `submit`): `Blocked` → `needs_user` with
  "SmartRecruiters blocked the app's browser — it treats our server as a bot. Apply on the job page
  in your own browser." The stop screenshot is kept as today. The run's `plan` stays empty.
- Platform name: Greenhouse / Lever from the adapter; for `generic` from the apply URL host via
  `sites.GENERIC_HOSTS` (SmartRecruiters, Ashby, …), else the job's ATS source, else the host.

## 2. Pause Auto apply for a blocked platform

- On `Blocked`, `apply_blocks.record(platform)` stores `until` in Redis hash `apply_blocked`
  (field = platform key, e.g. `smartrecruiters`). Pause `APPLY_BLOCK_PAUSE_HOURS` (24); a second block
  within a week doubles it, capped at `APPLY_BLOCK_MAX_PAUSE_HOURS` (168).
- **Server-wide**, not per user: every run comes from the same server IP, so a block hits everyone.
- `apply_options.options(job)`: while the job's platform is paused, `auto` =
  `{available: false, reason: "SmartRecruiters is blocking automated applications from our server
  right now. Apply manually — Auto apply is tried again after 27 Oct, 18:00."}`. The hash is read once
  and cached in-process for 30 s (the feed asks for many jobs at once).
- `create_run` checks the same thing → `409 platform_blocked` (a card loaded before the pause).
- After the pause, the next Auto apply click tries again; a clean form read clears the "repeat" count.
- Admin: `GET /api/v1/admin/system/health` lists paused platforms with `until` and the last run id;
  `DELETE /api/v1/admin/apply/blocks/{platform}` clears one (audited, `apply.block_clear`).

## 3. Wording on the review screen (frontend)

- `RunReviewPage.tsx` "Over to you": "(your answers are below to copy)" and "Copy all answers" only
  when the run has answers (`fields.length > 0`). The run's own `message` is shown as the reason
  ("SmartRecruiters blocked the app's browser…").
- Job cards need no change: the disabled Auto apply button already shows the backend's reason, and
  "Apply manually" stays the main button.
- Admin → System health: a "Paused for Auto apply" list with a Clear button (if decision 4 = yes).

## 4. Tests

- Backend: real Chromium on fixture pages (`tests/fixtures/apply/blocked_smartrecruiters.html`, a
  Cloudflare "Attention required" page, a page served with 403) → `needs_user` with the block message,
  platform paused; generic open → Apply click → block page; a normal page with the word "blocked" in a
  job description is NOT a block (patterns need the block-page phrases, not one word).
  `apply_options` reason while paused, `create_run` 409, doubling + cap, admin clear (+ audit).
- Frontend: RunReviewPage without answers has no "answers below" / "Copy all answers"; with answers
  unchanged. `npm run check` green.
- Then a live check: the Sia job → run ends with the new message; the card's Auto apply is disabled
  with the reason; Greenhouse/Lever cards unaffected.

## Order

Backend §1 → §2 → frontend §3 → tests in each → `pytest` + `npm run check` → commit on `dev`
(author Avanish Vishwakarma, no Claude trailers). No push until you say so.

## Decisions for the owner

1. **Pause scope:** server-wide per platform — *recommended* (the block is on the server IP, so it
   hits every user) — or per user?
2. **Pause length:** 24 h, doubling on repeat blocks up to 7 days — *recommended* — or a fixed 24 h?
3. **While paused:** Auto apply button stays visible but disabled with the reason — *recommended* —
   or hidden?
4. **Admin clear button** for a paused platform (e.g. after moving the server or the site lifting the
   block) — *recommended* — or let pauses only expire?
