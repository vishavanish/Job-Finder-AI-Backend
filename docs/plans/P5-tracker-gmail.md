# P5 — Tracker & Gmail: reply detection, rejection notes, needs-review inbox, Naukri alert emails

Status: **owner asked on 2026-10-19 to build the best option, so the recommended defaults in §6 are taken as approved. Backend built on `dev` — see CLAUDE.md 6.11.** The screens are F5 (separate plan).
Deviations: a reply in a Gmail thread the app already tracks skips the keyword prefilter (otherwise "Re: next round" was lost); the "Job alert emails" source shows as unavailable until a Gmail account is connected, so default searches don't list a source that can't run.
Inputs:
- CLAUDE.md §7.8 (tracker + Gmail pipeline), §12 (Gmail privacy), §15 (P5 acceptance).
- `../research/job-tools-research.md` §6: the free, safe Naukri path is the user's own Naukri job-alert emails.
- Today's `services/email_tracker.py`: Gmail tokens in files named per account, a keyword prefilter, and no classifier, matching or schedule.

Builds on P4b + F4 on `dev` (`adbc6b6`).

---

## 1. Goal and acceptance criteria

1. A user connects one or more Gmail accounts from the app (OAuth, **`gmail.readonly` only**). Tokens are stored **encrypted in the DB**, never in files. Disconnecting revokes the token at Google and deletes the account's synced data.
2. A scheduled sync reads only new mail and is **idempotent**: re-polling never duplicates an event.
3. Each likely job email is classified by the AI router (`email_classify`) as one of:
   - `acknowledgement`
   - `assessment`
   - `interview_invite`
   - `offer`
   - `rejection`
   - `job_alert`
   - `other`

   The result carries a confidence and the extracted company, role, dates and links.
4. The email is **matched to one of the user's applications** with a score: sender domain ↔ company, company in subject/body, role words, AI-extracted company, and the same thread as an earlier match.
   - A confident match plus a confident class **updates the application** through `record_status(status_source="email_parser")`.
   - Anything less goes to the **needs-review inbox** with up to 3 suggested applications.
   - A status never moves backwards (an acknowledgement after an interview is noted, not applied). Rejection and offer always apply.
5. **Rejection → `rejected` + a formal, neutral note**, e.g. "On 12 Oct 2026, Acme Corp told you it will not move forward with your application for Backend Engineer." It never quotes the email.
6. **Naukri (and other) job-alert emails become jobs**: links to job pages are extracted without AI, stored per user, and fed into searches as the source **"Job alert emails"** (dedupe, filters, scoring, seen-skip as usual). This is the free Naukri path.
7. **Tracker API:**
   - `GET /applications/stats`: funnel, this week, by status, by source;
   - `GET /applications/{id}/events`: timeline with source and note for each step;
   - the manual status change accepts the full status model.
8. **Privacy:**
   - stored: sender, subject, date, a ≤ 300-character snippet and the extracted fields only — **never full bodies**;
   - the body goes to the AI once for classification and is then dropped;
   - each status change shows the email that caused it;
   - events are deleted after `EMAIL_EVENT_RETENTION_DAYS` (180).
9. Offline tests (a fake Gmail service and fake AI) cover all of the above; the existing 239 tests stay green.

---

## 2. Design

### 2.1 Connect (per-user OAuth, web flow)
- **Setup:** one Google OAuth **web** client for the platform: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REDIRECT_URI` (the API's `/api/v1/gmail/callback`).
- **`GET /gmail/connect`** (entitlement `gmail_tracking`) returns Google's consent URL:
  - scopes `gmail.readonly openid email`;
  - `access_type=offline`, `prompt=consent`, PKCE.
- **State:** `state` is a random single-use id kept in Redis for 10 minutes with the user id and PKCE verifier. The callback trusts nothing else.
- **`GET /gmail/callback`** (a browser redirect, so there is no bearer token):
  1. checks and consumes the state;
  2. exchanges the code;
  3. requires `gmail.readonly` in the granted scopes;
  4. reads the address (`users.getProfile`);
  5. stores `email_accounts` with the refresh token sealed by `key_store.seal`;
  6. redirects to `APP_PUBLIC_URL/tracker?gmail=connected` (or `…=error&reason=`).
- **`GET /gmail/accounts`**, **`DELETE /gmail/accounts/{id}`**:
  - delete revokes the token at Google (best effort), then deletes the account, its events and its alert jobs;
  - applications keep their statuses and history.
- **Limits:** up to `GMAIL_ACCOUNTS_MAX_PER_USER` (3) accounts.
- **Clean-up:** the old file-token path (`token_<label>.json`, `GMAIL_ACCOUNT_LABELS`, `scripts/gmail_authorize.py`, `scripts/test_email_fetch.py`) is removed — CLAUDE.md 6.2 #6.

### 2.2 Sync
- A beat tick every `GMAIL_TICK_SECONDS` (300) claims due accounts with a conditional UPDATE on `next_sync_at` (same pattern as alerts). Each sync is one task on a new **`email` queue** (added to `worker_queues`).
- **First sync:** `messages.list q="newer_than:{GMAIL_INITIAL_LOOKBACK_DAYS}d -in:chats -in:sent -in:drafts"` (30 days, at most `GMAIL_MAX_MESSAGES_PER_SYNC`).
- **Later syncs:** `history.list(startHistoryId)`. If the history has expired (404), fall back to `after:<last sync>`.
- Messages are read as **metadata + snippet** first; the full body is fetched only for prefilter survivors.
- **Prefilter:** the existing high-recall hints + ATS sender domains + job-alert senders (Naukri, LinkedIn, Indeed, Foundit, Instahyre).
- **Idempotent:** `email_events` is unique on `(account_id, message_id)`.
- **Failures:**
  - a revoked/expired grant (`invalid_grant`) marks the account `needs_reconnect` and stops syncing it;
  - other errors back off, and 5 in a row pause the account for an hour.
  - The user sees both on the account.

### 2.3 Classify
- **Job-alert senders** are recognised without AI: `job_alert`, then the link parser (2.6).
- **Everything else:** one `email_classify` call through the router (Free tools only, model choice and the usage ledger apply). It gets the sender, subject and the first 3,000 characters of the body; prompt-injection guard as in other prompts. It returns:

  ```json
  {"category": "", "confidence": 0.0, "company": "", "role": "", "dates": [""], "links": [""], "summary": ""}
  ```
- **Fact rule:** the company/role must appear in the email (`grounding`), otherwise they are blanked.
- **No model:** the event is stored as `pending` and retried on the next sync (the same rule as the scoring fix).

### 2.4 Match
- **Candidates:** the user's applications from the last 180 days (any status but `opened`).
- **Score 0–100:**

  | Signal | Points |
  |---|---|
  | Same Gmail thread as an earlier matched event | +60 |
  | Normalised company (legal suffixes stripped, as `job_dedupe`) in the sender's domain | +35 |
  | Company in the subject or body | +30 |
  | AI company equals the application's company | +25 |
  | ≥ 50% of the role's words in subject/body (or the AI role) | +20 |
  | ATS sender (greenhouse / lever / workday …) | +5 |
- **Apply automatically** when the best score ≥ `EMAIL_MATCH_MIN_SCORE` (60), the next best is ≥ 15 points lower, and the confidence ≥ `EMAIL_CLASSIFY_MIN_CONFIDENCE` (0.7). Otherwise → `needs_review` with the top 3 candidates.
- **No candidate:** a rejection or interview with no candidate is `needs_review` with none (the user may have applied outside the app). `other` is stored as `ignored`.

### 2.5 Status update and notes
- **Category → status:**

  | Category | Status |
  |---|---|
  | `acknowledgement` | `acknowledged` |
  | `assessment` | `assessment` |
  | `interview_invite` | `interview` |
  | `offer` | `offer` |
  | `rejection` | `rejected` |
- **Order:** `submitted/emailed < acknowledged < assessment < interview < offer`. `rejected` and `offer` always apply; a lower status only adds a note to the timeline.
- **Rejection note:** built from a template — date, company and role from the match, never the email's wording. Other notes use the AI's one-line summary (fact-checked, ≤ 200 characters).
- **Audit trail:** `applications.last_email_snippet` is set, and the event keeps `application_id` and `applied_status`.

### 2.6 Job-alert emails → jobs (Naukri first)
- **Parse without AI:**
  - every link whose host is a known job board job page (`naukri.com/job-listings-…`, `linkedin.com/jobs/view/…`, `indeed.com/viewjob…`, `foundit.in/job/…`, `instahyre.com/job-…`);
  - the title is the link text;
  - company and location come from the next short text lines in the same block.
  - Tracking redirects are unwrapped when the target is in the URL.
- **Storage:** `email_jobs` (user_id, source, title, company, location, url, received_at; unique on user + url). Kept 30 days.
- **Search source:** `email_alerts` ("Job alert emails", kind `inbox`, free, on by default). `_scrape` loads the user's recent email jobs and `scrape_all` filters them by keyword like the remote feeds. Jobs keep their real source (Naukri…), so dedupe ranks and the Manual apply button work as usual.
- **Experimental:** the layout of Naukri alert emails isn't documented. The parser is tested on built fixtures and skips blocks it can't read; it needs checking against one real alert email after connecting.

### 2.7 Tracker API
- `GET /applications/stats?days=`:
  - `total`, `this_week`;
  - `funnel` {applied, acknowledged, assessment, interview, offer};
  - `rejected`;
  - `by_status`, `by_source`;
  - `needs_review` count.
- `GET /applications/{id}/events`: status, status_source, note, created_at, plus the email (sender, subject, date) when one caused it.
- `PATCH /applications/{id}` accepts every status in the CLAUDE.md §7.8 model (status_source `manual`).
- `GET /gmail/review`, `POST /gmail/review/{id}/resolve`:
  - `confirm` applies the suggestion;
  - `change` applies a chosen application and/or status;
  - `ignore` closes the item.
- `POST /gmail/accounts/{id}/sync` runs a sync now (rate-limited).

---

## 3. Steps
1. Migration `0010`:
   - `email_accounts`, `email_events`, `email_jobs`;
   - catalog row `email_alerts`;
   - `application_status_events.email_event_id`.
2. OAuth connect/callback/accounts/disconnect; tokens sealed; old file-token code removed.
3. Gmail client wrapper (list / history / get / profile / revoke) behind one small class so tests swap it.
4. Sync task + beat tick + `email` queue + failure handling.
5. Prefilter, classifier (router), grounding of extracted fields.
6. Matcher, status order, notes, `record_status`.
7. Job-alert parser + `email_jobs` + the `email_alerts` source in `scrape_all`.
8. Review inbox, tracker stats, events timeline, PATCH statuses, retention clean-up.
9. Tests:
   - OAuth state;
   - sync idempotency and history fallback;
   - classification and matching thresholds;
   - status order;
   - rejection note;
   - review resolve;
   - Naukri parser fixture;
   - inbox source in a pipeline run;
   - disconnect cascade;
   - EEO / bodies never stored.
10. CLAUDE.md §6.11, §7.8, §8–§10.

**New env:** `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REDIRECT_URI`, `GMAIL_TICK_SECONDS` (300), `GMAIL_SYNC_INTERVAL_SECONDS` (900), `GMAIL_INITIAL_LOOKBACK_DAYS` (30), `GMAIL_MAX_MESSAGES_PER_SYNC` (100), `GMAIL_ACCOUNTS_MAX_PER_USER` (3), `EMAIL_MATCH_MIN_SCORE` (60), `EMAIL_CLASSIFY_MIN_CONFIDENCE` (0.7), `EMAIL_EVENT_RETENTION_DAYS` (180), `EMAIL_JOBS_RETENTION_DAYS` (30), `RATE_LIMIT_GMAIL_SYNC` (6/hour).

---

## 4. Risks

| Risk | Mitigation |
|---|---|
| Google restricts `gmail.readonly` (a "restricted scope") for public apps: verification + a security assessment before more than 100 users | Works now for the owner and up to 100 test users (OAuth "testing" mode); verification is a launch task, noted in CLAUDE.md |
| Wrong match changes the wrong application | Score + margin + confidence thresholds; everything else goes to review; every change links to its email and can be changed back |
| AI cost per email | Prefilter first; job alerts parsed without AI; one call per surviving email; Free tools only respected |
| Naukri alert layout changes | Link-based parser, unknown blocks skipped, fixture tests, flagged experimental |
| Storing mail content | Only sender/subject/date/snippet/extracted fields; bodies dropped after classification; 180-day retention; delete on disconnect |

---

## 5. Not in P5
- Sending mail (Email apply as a Gmail draft, follow-up drafts, Find HR) — needs `gmail.compose`; later.
- Calendar events from interview invites (Appendix B).
- Outlook / IMAP accounts.
- Screens (F5).

---

## 6. Decisions (recommended defaults — taken as approved, 2026-10-19)
1. **Read-only Gmail** (`gmail.readonly`); no sending in P5. ✔
2. **Tokens encrypted in the DB**, file tokens removed. ✔
3. **Auto-update only when confident** (match ≥ 60 with a 15-point margin, class confidence ≥ 0.7); otherwise the needs-review inbox. ✔
4. **Statuses never move backwards**, except rejection/offer. ✔
5. **Rejection note from a template** (no AI wording, no quotes). ✔
6. **Job-alert emails → "Job alert emails" search source**, on by default, parsed without AI (Naukri first; LinkedIn/Indeed/Foundit/Instahyre links too). ✔
7. **Store snippets, never bodies**; events kept 180 days, alert jobs 30 days. ✔
8. **New `email` queue** for syncs (workers already take every queue from `worker_queues`). ✔
9. **Up to 3 Gmail accounts per user.** ✔
