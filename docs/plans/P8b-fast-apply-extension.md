# P8b — Fast apply in the user's own browser (extension "apply assist")

Status 2026-11-01: owner approved all eight defaults; **E3a and E3b built** (CLAUDE.md §6.22). E3c not started.
Difference from the plan: the form reader is a port of the server's (`extension/src/formreader.js`), not one shared
file — the two repos can't share code; keep them in step. Backend + frontend repo (`extension/`).

## 1. Problem

The owner sees only **Manual apply** on almost every job.

- **Auto apply** (server browser) matched only 14 hosted ATS sites. Fixed 2026-11-01 (`1879309`): any company
  career page a board posting links to now goes to the AI form reader. Still out of reach for the server:
  pages that need the user's own account (LinkedIn Easy Apply, Indeed apply, Naukri, Workday, iCIMS, Taleo,
  SuccessFactors, Oracle Cloud), and LinkedIn jobs, because JobSpy can't see their external apply link
  without a sign-in.
- **Fast apply** (LinkedIn Easy Apply) is refused in production: it needs the user's own signed-in LinkedIn.
  Keeping users' LinkedIn sessions on our server is ruled out (CLAUDE.md §7.7: fragile, risky, against
  LinkedIn's terms and the user's account safety).

## 2. Options considered

| Option | Verdict |
|---|---|
| A. Store each user's LinkedIn cookies on the server and run Easy Apply there | **No.** Session theft risk, LinkedIn flags datacentre IPs, breaks §7.7 / §12. |
| B. Desktop agent (Tauri app running Playwright on the user's PC) | Works, but a second install, a Chromium download (~150 MB), separate sign-ins, more code. Keep for later (it's also P7.1's Copilot client). |
| C. **The browser extension we already ship (P8a)** fills the form in the tab the user has open, signed in as themselves; the user clicks Submit | **Recommended.** Already installed and paired, no new sign-in, the user's own IP and session, nothing is submitted by us. Works on any site the user can open — LinkedIn, Workday, Naukri, Indeed. |

## 3. What the user sees (option C)

1. On a job card, **Fast apply** is clickable when the job is a LinkedIn Easy Apply job *and* the user has
   the extension connected. Without the extension it says "Install the Job Finder extension to use Fast
   apply" with a link to Settings › Browser extension (instead of today's "not available yet").
2. Clicking it starts an apply run (`mode: "fast"`, new `runner: "extension"`) and opens the LinkedIn job in
   a new tab.
3. In that tab the extension shows a small **Job Finder panel**: "Filling your Easy Apply…". It clicks **Easy
   Apply** once to open the dialog, then fills **each page as it appears**: contact details, resume,
   screening questions. The user clicks Next / Review / **Submit** themselves.
4. A question nobody can answer shows **in the panel** ("How many years of Kubernetes?") with a box. The
   answer is typed into the form and saved to the answer bank (approved), so it's never asked again.
5. When LinkedIn shows "Your application was sent", the extension marks the job **Applied** in Job Finder
   (tracker status `submitted`, source `extension`). If the user closes the tab, the run ends as
   `needs_user` with "Did you submit it?" (existing `POST /apply/runs/{id}/done`).
6. **Fill this form** (second step, E3b): on any application page the user is on — Workday after signing
   in, Naukri, Indeed apply, a company form the server couldn't open — the extension's button fills what it
   recognises the same way. Also offered from a `needs_user` Auto apply run ("Open and fill in my browser").

## 4. How it works

**Extension (frontend repo `extension/`, plain JS like E1)**
- `src/formreader.js`: the same field reader the server uses (`app/apply/forms.py` `_EXTRACT_JS`: label,
  type, options, required for text/select/radio/checkbox/combobox/file), moved to a shared file the server
  also loads, so both read forms identically. Tested in Vitest/jsdom with recorded Easy Apply and Workday
  fixtures.
- `src/filler.js`: sets values the way a person would (native value setter + `input`/`change`/`blur`
  events, so React forms like LinkedIn's accept them), picks options by text, ticks radios, uploads the
  resume through the file input (`DataTransfer` with the file fetched from the API).
- `src/linkedin_apply.js`: finds the Easy Apply dialog, watches it (`MutationObserver`) and fills each new
  page; detects the "application was sent" view. **Never clicks Next or Submit.** Clicking Easy Apply once
  to open the dialog is the only click.
- Manifest: content script on `linkedin.com/jobs/*` (already there); for "Fill this form" the
  **`activeTab`** permission (runs only on the tab where the user pressed the button — no `<all_urls>`).

**Backend**
- Extension token gains a second scope, `extension:assist` (today: capture only). Routes:
  - `GET /extension/runs/next?url=` → the user's waiting fast run for this job page (or `none`);
  - `POST /extension/fill {run_id?, page_url, fields}` → answers through `answer_bank.answer_fields` (profile
    → EEO → answer bank → AI, grounded; unknown → `needs_input`), never more than one page's fields;
  - `POST /extension/answer {run_id, field, value}` → saves to the answer bank (approved);
  - `GET /extension/runs/{id}/resume` → the tailored resume from the prep pack if ready, else the master
    (DOCX/PDF bytes, owner only);
  - `POST /extension/runs/{id}/result {outcome: submitted|closed}` → `record_status(status_source="extension")`.
- `apply_engine.create_run(mode="fast")`: in production allowed when `extension.has_connection(user)`
  (runner `extension`, state `awaiting_user`, no Celery task); in dev the Playwright path stays.
- `apply_options`: stays a pure function of the job; the routes add the per-user part
  (`fast.available` = LinkedIn Easy Apply + extension connected, else the install reason).
- Caps unchanged: `FAST_APPLY_DAILY_CAP` (25/day), duplicates, company cooldown. AI answers count in the
  usage ledger as today.

**More Auto apply jobs from LinkedIn (small, E3c)**: while the user views a LinkedIn job signed in, the E1
reader also reads the external **Apply on company website** link (shown to signed-in users) and sends it with
the capture; `job_store` stores it as `apply_url`, so the job gets Auto apply when it's a company page.

## 5. Safety

- The user submits every application. No captcha, sign-in or 2FA is touched; if LinkedIn shows one the
  panel says "LinkedIn wants you to check something — do it, then press Continue".
- EEO answers are filled from the user's saved answers but never sent to the AI (as today).
- The token can only call the `/extension/*` routes; revoked in Settings or when the account is disabled.
- Risk notice (shown once before the first Fast apply): automating parts of LinkedIn can break its terms;
  the app fills, you review and submit; caps stay on.

## 6. Steps

| Step | Scope | Done when |
|---|---|---|
| E3a | Shared form reader, filler, LinkedIn Easy Apply panel, backend routes + `runner: extension`, Fast apply button in production, tracker update | A recorded Easy Apply flow (3 pages, a screening question, resume) fills in jsdom; backend tests for scope, caps, answers, result; one real Easy Apply filled by the owner (owner submits) |
| E3b | "Fill this form" on any page (`activeTab`), "Open and fill in my browser" from a stopped Auto apply run | Workday + Greenhouse fixtures fill; owner tries one Workday form |
| E3c | External apply link from LinkedIn job pages → `apply_url` | A captured LinkedIn job with a Greenhouse link shows Auto apply |
| Later | Store publishing (Edge free, Chrome US$5 once), Firefox | — |

Estimate: E3a is the biggest (≈ the size of P4c); E3b and E3c are small.

## 7. Also recommended (outside the extension)

- After ~1–2 weeks of test-mode Auto apply runs look right, set `GENERIC_APPLY_SUBMIT=true` on the server.
- Let `GET /apply/runs/{id}` show the stop reason (`apply_runs.error`) to the user — today it's only in the
  database.

## 8. Decisions for the owner (recommended defaults)

1. Fast apply runs in the user's own browser through the existing extension (option C), not on the server and
   not as a desktop app yet. _Recommended: yes._
2. The extension fills each Easy Apply page as it appears; the **user clicks Next and Submit**. _Recommended:
   yes_ (the alternative — the extension also clicks Next — is faster but more like a bot to LinkedIn).
3. Unknown questions are asked in the panel and saved to the answer bank as approved. _Recommended: yes._
4. Resume: the tailored one from the prep pack when it's ready, else the master. _Recommended: yes._
5. Existing paired extensions get the `extension:assist` scope after updating, with a switch in Settings ›
   Browser extension to turn apply assist off. _Recommended: yes_ (no re-pairing).
6. Build E3a first, then E3b ("Fill this form" anywhere) and E3c (LinkedIn external links). _Recommended: yes._
7. Mark the job Applied automatically when LinkedIn confirms ("application was sent"). _Recommended: yes._
8. One-time risk notice before the first Fast apply. _Recommended: yes._
