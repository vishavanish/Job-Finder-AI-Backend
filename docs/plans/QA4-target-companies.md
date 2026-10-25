# QA4 — Target companies (follow companies, get told first)

Status: **approved and built** (2026-10-25, recommended defaults). See CLAUDE.md §6.17. Builds on QA3
(`docs/plans/QA3-ats-in-job-search.md`) — do QA3 first.

## 1. Why

After QA3, Job Search already brings career-page jobs, so the ATS page as a second search box adds little. What
Job Search can't do is **watch the companies a user wants to work for** and tell them the moment a matching role is
posted — usually before LinkedIn/Naukri carry it, with fewer applicants, and often auto-applyable (Greenhouse/Lever).

Today the ATS page has no follow, no per-user list, no alerts, no suggestions (checked 2026-10-25; the pre-F6
browser-only "targets" list was never searched and was retired in F6).

## 2. What the user gets

**Target companies page** (the ATS page, renamed):

| Section | What it does |
|---|---|
| My companies | Followed companies: open matching roles, newest post time, **"New" badge** for roles since the last visit, Unfollow |
| Suggested for you | "Hiring for your skills": companies with the most open matching roles in the user's places, plus companies of jobs they saved/applied to. Follow button. |
| Browse companies | Today's title search grouped by company (kept), with Follow on each card |
| Company detail | Open roles, careers link, platform, per-job Prepare / Auto apply / Track (existing buttons) |
| Follow by name or link | Type "Razorpay" → we find its career page (QA3 resolver); or paste a careers link (existing add) |
| Alert settings | Instant (batched, at most hourly) / Daily (one email at 9:00 local) / Off; which titles count (default: titles from the default search profile) and places (default: Profile places + Remote) |

**Elsewhere:**
- **Jobs:** jobs from followed companies show a ★ "Company you follow".
- **Dashboard:** "New at your companies" card (latest 5 hits, link to the page).
- **Email:** plain-text digest using the existing OTP/alert email settings.
- Apply Center / Tracker: unchanged — applications from these jobs appear like any other.

## 3. How it works (backend)

### 3.1 Tables — migration `0017_target_companies`
- `ats_follows`: user_id, company_id, created_at, last_seen_at (for "New"); unique (user_id, company_id).
- `ats_follow_hits`: user_id, company_id, ats_job_id, created_at, emailed_at, seen_at; unique (user_id, ats_job_id).
  Purged after 30 days or when the job closes.
- `ats_follow_settings`: user_id (pk), alert_mode (`instant` | `daily` | `off`), titles (JSON, empty = use default
  search profile titles), places (JSON, empty = Profile places + Remote), timezone (default Asia/Kolkata).
- `ats_companies.follower_count` (int, default 0) — kept in step with follows; drives refresh speed.

### 3.2 Faster checks for followed companies
- `ats_crawler._next_crawl`: a company with followers refreshes every `ATS_FOLLOWED_REFRESH_HOURS` (default **3 h**)
  instead of 6 h hot / 24 h cold. Cost is bounded by how many distinct companies are followed.

### 3.3 Detecting new roles
- `crawl_company` already knows which rows were **added**. When the company has followers, it queues
  `ats_follow_match(company_id, new_job_ids)` (maintenance queue).
- The task matches each new job against each follower's titles with the existing title matcher
  (`ats_search.expansions` + synonyms, **no AI call**) and places filter; matches become `ats_follow_hits`.
- No AI scoring at alert time (free models are rate-limited). The score appears when the user opens / tracks the job,
  through the normal path.

### 3.4 Sending alerts
- Beat tick `ats_follow_digest` (every 15 min, maintenance queue): instant users with un-emailed hits and no email in
  the last hour get one digest; daily users get it at 09:00 local. Off = page and Dashboard only.
- Digest: "New at Razorpay: Backend Engineer (Bengaluru), posted 2 h ago" + link to the page. Max 20 roles per email.
- Entitlement `ats_discovery` (existing); disabled users skipped.

### 3.5 Suggestions
- `GET /ats/suggestions`: top 12 companies by open matching roles (user titles × places, last 30 days) not already
  followed, plus registry companies matching names of the user's saved/applied jobs. Cached per user 24 h.

### 3.6 API (all under `/api/v1`, `require_feature("ats_discovery")`)
- `GET /ats/following` — followed companies with `open_matching`, `new_count`, `latest_posted_at`.
- `PUT /ats/companies/{id}/follow`, `DELETE /ats/companies/{id}/follow`.
- `POST /ats/following/by-name {name}` — resolver lookup (QA3); returns the company or `{status: "searching"}` and
  follows it once found.
- `GET /ats/following/new` (hits), `POST /ats/following/seen {company_id?}`.
- `GET /ats/suggestions`.
- `GET/PUT /ats/following/settings`.
- Feed + `GET /jobs`: `followed: true` on jobs whose company is followed.
- Limit `TARGET_COMPANIES_MAX_PER_USER` (default **50**); `RATE_LIMIT_ATS_FOLLOW` (60/hour).

## 4. Decisions (recommended default in bold)

1. Page name and menu: **"Target companies", back in the main Workspace menu** (it now does something Job Search
   can't) / keep it as the optional "Browse companies" from QA3.
2. Refresh for followed companies: **every 3 h** / 6 h / 1 h.
3. Alert default for a new follower: **Daily at 09:00** / Instant / Off.
4. Match by: **titles from the default search profile (editable)** / any open role at the company.
5. AI score in alerts: **no (title match only; score when opened)** / yes (uses free AI quota).
6. Max followed companies per user: **50** / 20 / unlimited.
7. Follow by name: **yes, using the QA3 resolver** / careers link only.

## 5. Build order (each its own commit on `dev`)

1. Migration `0017` + follow/unfollow/list/settings API + `follower_count` + 3 h refresh. Tests.
2. Match task on new jobs + hits + digest tick + email. Tests: new job → hit for a matching follower only; place
   filter; hourly batching; daily at 09:00 local; off; closed job drops its hit; disabled user skipped.
3. Suggestions + follow by name (needs QA3 resolver) + `followed` flag in the feed. Tests.
4. Frontend: Target companies page (My companies, Suggested, Browse, detail, settings), ★ on Jobs, Dashboard card
   (`npm run check`).
5. Blueprint `CLAUDE.md` §6.17 + env docs.

New env (optional): `ATS_FOLLOWED_REFRESH_HOURS` (3), `TARGET_COMPANIES_MAX_PER_USER` (50),
`ATS_FOLLOW_DIGEST_TICK_SECONDS` (900), `ATS_FOLLOW_HIT_RETENTION_DAYS` (30), `RATE_LIMIT_ATS_FOLLOW` (60/hour).

## 6. Risks
- Email volume: hourly cap per user, max 20 roles per email, Off option.
- Crawl load: only followed companies speed up; the per-host slots and `ATS_HOST_CONCURRENCY` still apply.
- A followed company not on a supported platform (e.g. Darwinbox / Keka / Zoho — P6.1): follow-by-name says
  "career page not supported yet" and keeps the request so it is picked up when P6.1 adds the platform.
