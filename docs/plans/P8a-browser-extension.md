# P8a — Browser extension "Job Finder capture" (plan, waiting for owner approval)

Owner asked 2026-10-31 for a free way to get jobs from Naukri, Foundit, Glassdoor, ZipRecruiter, Darwinbox
and PeopleStrong. From the server those sites answer with captchas, 403s or a browser-only page, and
CLAUDE.md §12 forbids working around that. The allowed, free route: the **user's own browser**, while the
user is looking at the page, saves the jobs that are already on screen. No captcha is solved, no request is
disguised, nothing runs in the background on its own.

## 1. What the user sees

1. Settings → **Connect browser extension** → install it (Chrome / Edge; Firefox later) → the app shows a
   6-digit pairing code → typed into the extension once.
2. On a supported job-search page (e.g. a Naukri search for "Python developer, Pune"), the extension shows
   a small **"Save N jobs to Job Finder"** button. One click saves the jobs visible on that page.
3. Saved jobs appear in Job Search like any other source ("Saved from Naukri"), are de-duplicated (LinkedIn
   copy still wins), scored against the resume, and can be applied to manually from the same card.
4. Optional (off by default): **"Save automatically while I browse"** for sites the user switches on — still
   only pages the user opens, at most one save per page view.

## 2. Sites (first release → later)

| Release | Sites | How the jobs are read from the page |
|---|---|---|
| E1 | Naukri, Foundit, Glassdoor, LinkedIn Jobs, Indeed | Visible job cards (DOM) and the page's own embedded data (JSON-LD `JobPosting`, inline JSON) |
| E2 | Darwinbox, PeopleStrong career portals, Instahyre, Cutshort, Wellfound, Hirist/iimjobs, ZipRecruiter | Same; career portals add the company name from the page |
| E3 | Apply assist: fill forms in the user's browser from the profile + answer bank (never submits) | Reuses `answer_bank.answer_fields` through the API |

## 3. Backend

- `POST /api/v1/extension/pair` (web app, signed in) → one-time code (10 min, Redis); `POST
  /api/v1/extension/token {code}` → a long-lived **capture-only** token (scope `extension:capture`, revocable,
  listed in Settings, never usable for other routes).
- `POST /api/v1/extension/captures {site, page_url, jobs: [{title, company, location, url, posted, description?}]}`
  — ≤ 100 jobs, each URL must be on the declared site's hosts, text sanitised and length-capped; no HTML,
  cookies or page content beyond these fields are accepted or stored.
- Jobs go through `job_store.upsert_copy` (normal dedupe + source rank), `user_jobs` state `saved`, and the
  normal scoring queue. New source **`browser_capture`** ("Saved from your browser", kind `inbox`), like
  `email_alerts`: on by default, available once an extension is connected.
- Entitlement `browser_capture` (on), rate limit (e.g. 60 captures/hour), audit of pair/revoke.

## 4. Extension (new folder `extension/` in the frontend repo, TypeScript + Vite, Manifest V3)

- Content scripts only on the supported hosts (declared in the manifest — no `<all_urls>`), one parser per
  site with recorded HTML fixtures and tests (like the ATS adapters).
- Never: opens tabs, clicks "next page", scrolls by itself, fills captchas, reads cookies or other sites.
- Token in `chrome.storage.local`; popup shows connection, today's saves, per-site switches, "Disconnect".

## 5. Cost and limits

- Free to build and to use. Publishing on the Chrome Web Store needs a **one-time US$5 developer fee**
  (Edge Add-ons is free); until then it can be loaded unpacked from a zip.
- Page layouts change: each site parser is a small file with fixtures; a broken parser shows "This page
  isn't supported right now" instead of saving wrong data.
- Some sites' terms discourage automated collection even from your own browser. The default (one click per
  page, no background activity) is the most conservative choice; the auto-save switch is off by default
  with a notice.

## 6. Decisions for the owner (recommended defaults)

1. Build E1 first (Naukri, Foundit, Glassdoor, LinkedIn, Indeed). _Recommended: yes._
2. Chrome + Edge first, Firefox later. _Recommended: yes._
3. Manual "Save jobs" button by default; auto-save off, per site. _Recommended: yes._
4. Code lives in the frontend repo under `extension/` (shared types, one CI). _Recommended: yes._
5. Publish to Edge Add-ons (free) and Chrome Web Store (US$5 once) after a private test week. _Recommended: yes._
6. E3 apply assist only after E1/E2 are stable. _Recommended: yes._
