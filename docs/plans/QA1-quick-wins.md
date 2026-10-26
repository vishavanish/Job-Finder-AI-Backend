# QA1 — Phase 1 quick wins from the live QA pass (2026-10-25)

Source: live QA of https://job-finder-ai-frontend-one.vercel.app on 2026-10-25.
Scope: the seven fixes the owner picked. Both repos, branch `dev`. Nothing is pushed or
deployed without the owner asking.

Out of scope here (later): `/health` lockdown, search speed, ATS data cleanup, resume
review, mobile pass.

---

## 1. Session lost on every reload (critical)

**Cause.** The refresh cookie `jf_refresh` is set by `jobfinderai.duckdns.org`; the app runs
on `*.vercel.app`. Browsers treat it as a third-party cookie and drop it (confirmed in
Chrome: `POST /auth/refresh` → `401 "No refresh token"`).

**Fix (frontend repo).**
- `vercel.json`: add `{"source": "/api/:path*", "destination": "https://jobfinderai.duckdns.org/api/:path*"}`
  **before** the SPA catch-all. The browser then talks only to the Vercel host, so the
  cookie is first-party.
- `.env.production`: `VITE_API_BASE_URL=` (empty = same origin; `lib/env.ts` already supports it).
- New `VITE_WS_ORIGIN=https://jobfinderai.duckdns.org`: Vercel rewrites cannot proxy
  WebSockets, so `socketUrl()` in `features/interview/protocol.ts` uses this origin for voice
  sockets. Those authenticate with a stream token, not the cookie, so they keep working.
- Tests: env/socketUrl unit tests.

**Backend.** No code change needed: CORS stays for direct calls and Gmail OAuth still
redirects through `GMAIL_REDIRECT_URI` on duckdns.

**Known trade-offs (accepted for now, listed so nobody is surprised):**
- The API sees Vercel's IPs, not the user's. Per-IP rate limits (OTP, uploads) become shared
  across users. Per-email OTP limits still hold. Fine at today's traffic; the custom domain
  (below) removes this.
- Vercel limits proxied requests (body size ~4.5 MB, response timeout). Resume uploads are
  ≤ 5 MB → **lower `RESUME_MAX_BYTES` to 4 MB** in the server `.env`, or accept rare failures.
  Verify a real upload after deploy.

**Later, clean option:** a custom domain (`app.<domain>` on Vercel, `api.<domain>` on the
server). Same-site cookie, real client IPs, no proxy limits. Costs a domain.

**Done when:** sign in → reload → still signed in, in Chrome and Safari/Incognito.

## 2. Google sign-in fails without an error (critical)

**Owner step (2 min):** Google Cloud Console → project `sso-job-finder` → APIs & Services →
Credentials → OAuth client `776583907200-shlkg5…` → Authorized JavaScript origins → add
`https://job-finder-ai-frontend-one.vercel.app` → Save (takes a few minutes).

**Code (frontend):** `GoogleButton.tsx` passes GSI's `click_listener`; if no credential
arrives within 20 s after a click, show "Google sign-in didn't finish. Try again, or get a
code by email." Test with a fake GSI.

## 3. AI models all paused (critical)

**Cause.** `ai_router._record_failure` counts every failure the same: rate limits (429),
replies that didn't parse, and real outages. 3 in a row pause the model for 5 min **for
everyone**. A single search scoring many jobs in parallel trips every free model, then
resume tailoring, cover letters and interview practice all fail. Last 30 days: 93 of 213
calls failed; Qwen3 23/23, Gemini 3.7 14/15, Gemini 3.8 15/20.

**Fix (backend `ai_router.py`, tests in `tests/test_ai_router*.py`):**
- Classify the failure:
  - **rate limit** (429 / `RESOURCE_EXHAUSTED` / "quota") → pause that model for
    `AI_RATE_LIMIT_PAUSE_SECONDS` (60), not the 5-min breaker.
  - **bad reply** (empty, unparseable, failed validation) → the model is up; don't count it
    toward the breaker.
  - **broken setup** (401/403/404, model not found, package missing) → pause
    `AI_BROKEN_PAUSE_SECONDS` (3600) and keep the reason in `ai_models.last_error` for the admin.
  - everything else (timeouts, 5xx) → today's breaker.
- **Never zero models:** if every model in the chain is paused, try the one whose pause ends
  first (one probe) instead of failing instantly.
- Cap parallel scoring calls per provider (Redis slot, `AI_PROVIDER_CONCURRENCY` = 2) so one
  search can't burn a free tier.

**Owner steps (Admin → AI models / Platform keys), after 1 decision below:**
switch off Qwen3 8B and Gemini 3.7 Flash; add an OpenRouter platform key and switch on the
OpenRouter fallback row.

**Frontend:** a small banner on Jobs, Apply Center and Interview prep when every text model is
paused ("AI is busy — scoring and writing resume back in ~N min"), from `GET /models` health.

## 4. Search results disappear on navigation (critical)

**Cause.** The current run id lives only in `JobsPage` state; leaving the page drops it.
Jobs already shown count as "seen", so re-running doesn't show them again.

**Fix:**
- Backend: `GET /pipeline/runs/latest` → the user's newest run in Redis (the
  `user_runs` set already exists; runs live 6 h) with `run_id`, status, `skipped_sources`,
  started time; 404 when none. Test.
- Frontend: Jobs loads the latest run on open and re-reads its jobs (`usePipelineRun` from
  offset 0); Activity "Open" links to `/jobs?run=<id>`; a 404 / expired run shows "Your last
  search has expired — saved jobs are under Saved." Tests with MSW.

## 5. Disabled "Auto apply" looks like the main button

`ApplyButtons.tsx:112`: primary style only when the option is available; otherwise
`btn-secondary`. When Auto isn't available, "Apply manually" stays the primary button. Also add
"Mark as applied" on the card (not only after "Open job page" in the dialog). Tests.

## 6. "11 sources will run" isn't true

**Cause.** `provider_catalog.availability` says "will run" for Naukri (manual paste-in) and
Naukri via Apify (only runs with its own flag), but `effective_sources` never runs them in a
normal search.

**Fix (backend):** one helper `runs_in_default_search(...)` shared by `effective_sources` and
`GET /providers`, so the page and the search agree. Manual: "Paste jobs in — not searched
automatically." Apify: "Runs only when Naukri via Apify is switched on for a search." Fix
"needs a adzuna API key" → "needs an Adzuna API key" (display names). Tests.

## 7. Raw AI errors in the Apply dialog

**Backend (`apply_prep.py`):** `pack.error` gets a short user message
("The AI is busy right now — try again in a few minutes."); the model-by-model detail moves
to `pack.error_detail` (new nullable column + migration `0015`, shown only in a "Details"
fold).
**Frontend (`PrepDialog.tsx`):** one message (no red box + grey repeat), a **Try again**
button, details folded. Tests.

---

## Order and checks

1. Backend: §3 router, §4 latest run, §6 sources, §7 prep error (+ migration 0015). `pytest` green.
2. Frontend: §1 env/rewrite/WS, §2 Google timeout, §3 banner, §4 restore, §5 buttons, §6 count,
   §7 dialog. Regenerate API types; `npm run check` green.
3. Browser check on mocks (`frontend-mocks`) for §2–§7 UI.
4. Commit on `dev` in each repo (author Avanish Vishwakarma, no Claude trailers). **No push.**
5. Going live (owner, when ready): merge backend to `main`, server `git pull` + `alembic
   upgrade head` + restart; frontend production deploy; Google origin; admin model steps.
   Then I re-test the live site (reload, Google, search → navigate → back, prep pack).

## Decisions for the owner

1. **Cookie:** Vercel rewrite now (free; shared-IP and ~4.5 MB limits) — *recommended* — or
   buy a domain and do it the clean way first?
2. **AI errors data:** may I read the last 100 failed AI calls from the production
   `usage_ledger` (read-only SELECT, no user content — error text, model, time) to tune the
   classification? Otherwise I build from the adapter code alone.
3. **Resume size:** lower `RESUME_MAX_BYTES` to 4 MB on the server — *recommended* — or keep
   5 MB and accept that a rare 4.5–5 MB upload fails through the proxy?
4. **Migration 0015** for `apply_packs.error_detail` — *recommended* — or squeeze the detail
   into the existing `error` text with a separator (no migration)?
