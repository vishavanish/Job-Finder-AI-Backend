# P6 — ATS Discovery: company registry, crawler, job index, title search grouped by company

Status: **owner approved all 10 decisions in §7 on 2026-10-19; built on `dev` 2026-10-20** (CLAUDE.md §6.12). Differences from this plan: BambooHR and Homerun not built (BambooHR's careers JSON redirected for every tenant tried; Homerun's public feed returns its own jobs for any company name); platform on/off + health live in a new `ats_platforms` table instead of `provider_catalog` rows (catalog rows show up as sources on users' Providers page); the seed is whatever the probe confirmed, not a fixed 3,000. Revised 2026-10-19: owner asked for **more ATS platforms** to get more company jobs (§3.0). Backend first; the screen is F6 (separate plan).
Inputs:
- CLAUDE.md §7.6 (ATS Target Discovery), §8 (`ats_companies`, `ats_jobs`), §15 (P6 acceptance).
- `../research/job-tools-research.md` §7.1 and item 15: public ATS feeds are the biggest free job source; "the trick is the company list".
- Today's `services/company_ats_sources.py`: fetchers for 11 platforms, called only by `POST /company-ats/fetch` with slugs the user types. No registry, no index, no schedule, and company ATS is **not** a source in normal search runs.

Builds on P5 (migration `0010`). P5 is still uncommitted on `dev`; P6 starts after it is committed.

---

## 1. Goal and acceptance criteria

**User story:** "I type a job title. I see every company in the registry hiring for it on any of 12 ATS platforms (Greenhouse, Lever, Workday, Recruitee, Ashby, SmartRecruiters, Workable, …), grouped by company, with links — and those jobs also show up in my normal searches and alerts, where Auto apply already works for Greenhouse and Lever."

Acceptance (CLAUDE.md §15 plus what makes it useful):
1. `GET /ats/search?q=<title>` returns companies grouped, across all indexed platforms (13 in P6), **in under 3 s from the index** (p95, Postgres, 300k-job index).
2. Filters: platform, location / remote, posted within, hide already applied.
3. The response shows registry size, indexed job count and last refresh time (honest scope, §7.6.6).
4. If the index has few matches, a live top-up runs in the background and the client polls for more.
5. A new `company_ats` source puts index jobs into pipeline runs and alerts (dedupe rank "company ATS" already exists), so they get scored and get the Apply Center buttons.
6. The registry grows on its own: apply links seen in other sources, companies users add, and an admin seeding script.
7. Crawling is polite: conditional requests, per-host limits, backoff, dead slugs switched off.

---

## 2. Live check (2026-10-19, from this machine)

| Endpoint | Result |
|---|---|
| Greenhouse `boards-api.greenhouse.io/v1/boards/{slug}/jobs` | ✅ 200, 0.9 s; sends `ETag` and answers `If-None-Match` with **304** |
| Lever `api.lever.co/v0/postings/{slug}?mode=json` | ✅ 200 (palantir, spotify); **404 for a slug that left Lever** (plaid) |
| Recruitee `{slug}.recruitee.com/api/offers/` | ✅ 200, 2.4 s, ~680 KB (descriptions included) |
| Workday `POST {host}/wday/cxs/{tenant}/{site}/jobs` | ✅ 200, 3.2 s, accepts `searchText` |
| Ashby `api.ashbyhq.com/posting-api/job-board/{slug}` | ✅ 200, 2.8 MB (heavy) |
| SmartRecruiters `api.smartrecruiters.com/v1/companies/{slug}/postings` | ✅ 200 (BoschGroup: 5,083 jobs — needs a per-company cap) |
| Personio `{slug}.jobs.personio.de/xml` | ✅ 200 XML |
| Breezy `{slug}.breezy.hr/json` | ✅ 200 JSON |
| Workable `apply.workable.com/api/v3/accounts/{slug}/jobs` | existing fetcher (tested in P3.1); today's test slug was wrong |
| **Teamtailor** `{host}/jobs.rss` (new) | ✅ 200 RSS with locations |
| **Rippling** `ats.rippling.com/api/v2/board/{slug}/jobs` (new) | ✅ 200 JSON (title, department, locations, url) |
| **Gem** `api.gem.com/job_board/v0/{slug}/job_posts/` (new) | ✅ 200 JSON, Greenhouse-like shape with content |
| **Homerun** `feed.homerun.co/jobs?company={slug}` (new) | ✅ 200 Atom feed |
| BambooHR `{slug}.bamboohr.com/careers/list` (new) | ⚠️ JSON per company per docs; test slug redirected — verify with a real tenant during build |
| Darwinbox, Keka, Zoho Recruit, Freshteam (big in India) | ⚠️ script-driven pages only; each needs its own JSON call worked out from the page → P6.1 |
| Jobvite, iCIMS, JazzHR, Pinpoint | ❌ no clean public feed found today (HTML / 404) → later, through the generic page reader |
| Common Crawl CDX index | ❌ 504 timeouts — usable only as an offline, retrying seeding script, never at request time |

---

## 3. Design

### 3.0 Platforms (owner: more ATS tools = more company jobs)

| Tier | Platforms | In P6? |
|---|---|---|
| **A — existing fetchers, indexed** | Greenhouse, Lever, Workday, Recruitee, Ashby, SmartRecruiters, Workable, Personio, Breezy | ✅ |
| **B — new fetchers, public feeds verified today** | Teamtailor, Rippling, Gem, Homerun | ✅ |
| **C — new, verify with a real tenant first** | BambooHR | ✅ if verified, else dropped with a note |
| **D — India HR suites** | Darwinbox, Keka, Zoho Recruit, Freshteam | **P6.1** (one adapter each, read-only JSON the career page itself calls; never captcha/login pages) |
| **E — per-company URL** | SuccessFactors, Oracle Fusion (fetchers exist, need a session/careers URL) | ✅ admin/user-added only, not discovered automatically |
| Later | Jobvite, iCIMS, JazzHR, Pinpoint | generic page reader (P8) |

- One `AtsPlatform` interface per platform: `parse_url(url) → slug`, `probe(slug)`, `list_jobs(slug, etag)`, `search(slug, text)` (optional, Workday/SmartRecruiters), `job_detail(slug, id)`. Adding a platform = one file + recorded fixture + seed rows; no change to crawler or search.
- Each platform is a row in `provider_catalog` (`ats_<platform>`, tier free) so an admin can switch one off or see its health, and the breaker pauses a failing platform.
- **Per-company cap** `ATS_MAX_JOBS_PER_COMPANY` (1,000). For bigger tenants (Bosch 5k, NVIDIA 2k) only jobs in the platform's target markets (`ATS_INDEX_COUNTRIES`, default India + remote) are indexed; the rest are reached through live `search` when a user asks.

### 3.1 Registry (`ats_companies`)
- Columns: `platform`, `slug`, `workday_host`, `workday_site` (Workday only), `name`, `careers_url`, `origin` (`seed` / `pipeline` / `user` / `admin`), `active`, `added_by_user_id`, `job_count`, `etag`, `content_hash`, `last_crawled_at`, `last_ok_at`, `next_crawl_at`, `fail_count`, `last_error`.
- Unique `(platform, slug, workday_site)`.
- The registry is **shared by all users**: a company one user adds helps everyone. Only the public job list is stored.

**How it grows**
1. **Seed file** `app/data/ats_seed.json` (~3,000 companies to start across tiers A–C, India + remote-friendly mix), built by `python -m app.scripts.discover_ats` from Common Crawl, open slug lists and hand-picked companies. Every entry is probed before it goes in. Loaded with `.\admin ats-seed`.
2. **From pipeline results:** when any source returns a job whose apply link is on any supported ATS host, the slug is parsed with `detect_platform` and queued for a probe. Free, and it follows what users actually search for.
3. **User adds a company:** `POST /ats/companies` with a careers URL or `platform + slug`; SSRF guard (also after redirects), probe, then shared. Rate-limited.
4. **Admin:** add, edit, switch off; run discovery.

### 3.2 Crawler
- Beat tick every `ATS_CRAWL_TICK_SECONDS` (300) picks companies whose `next_crawl_at` is due, on the `maintenance` queue, at most `ATS_CRAWL_BATCH` (50) per tick.
- **Tiered refresh:** "hot" companies (matched a search in the last 7 days, or have a saved/applied job) every 6 h; others every 24 h.
- **Polite:** `If-None-Match` / `If-Modified-Since` when the host sends them (Greenhouse does: 304 = nothing to write); otherwise compare a hash of the response. At most 2 parallel requests per host; exponential backoff on 429/5xx; `fail_count` ≥ 3 for 404 → company switched off (slug left the platform).
- **Workday:** list pages up to `ATS_WORKDAY_MAX_JOBS` (400) per tenant per crawl; big tenants are also searched live with `searchText` (§3.4).
- Upsert by `(company_id, external_id)`; jobs missing from a full crawl get `closed_at`, and closed jobs are deleted after 7 days.
- Reuses the existing fetchers; Greenhouse switches to the list call without `content=true` (much lighter), descriptions fetched on demand.

### 3.3 Job index (`ats_jobs`)
- Columns: `company_id`, `external_id`, `title`, `title_norm`, `location`, `country`, `remote`, `department`, `url`, `posted_at`, `first_seen_at`, `last_seen_at`, `closed_at`, `snippet` (≤ 300 chars), `job_key` (from `job_dedupe.job_key`, for "already applied" and pipeline dedupe), `content_hash`.
- **No full descriptions in the index.** At ~300k jobs, full text would blow Supabase's free 500 MB; a short row is ~0.5 KB. Full text is fetched when a job is opened, scored, or sent to Apply.
- Postgres: generated `tsv` column (title weighted A, department B, snippet C) + GIN index; btree on `(country, remote, posted_at)`. SQLite (local + CI): no `tsv`; search falls back to token `LIKE` on `title_norm`.

### 3.4 Search
1. Normalise the title (lowercase, strip seniority noise, common abbreviations: SDE → software engineer, PM → product manager, ML → machine learning…).
2. **Expand synonyms:** a built-in table first (works under Free tools only), then one AI-router call for titles it doesn't know, cached in Redis for 30 days per normalised title.
3. Query the index (`websearch_to_tsquery` OR'd over the expansions), apply filters, rank with `ts_rank` + recency, **group by company**: company, platform, careers URL, matching count, top 3 jobs, locations, newest posted date. Page of 25 companies.
4. Cache each `(query, filters)` result for 10 minutes.
5. **Live top-up:** fewer than `ATS_MIN_COMPANIES` (10) companies → dispatch a task that live-searches stale companies (Workday with `searchText`, others full list), at most 40 companies, 8 at a time, 20 s budget, writing into the index. Response carries `topup_task_id`; the client polls and re-queries.
6. Every search marks matched companies "hot" (§3.2).

### 3.5 Pipeline and alerts
- New registry source `company_ats` (kind `index`, free, on by default): each keyword runs the same index query, returns jobs in the normal job shape. Then dedupe → keyword prefilter → scoring as usual.
- Full description fetched only for jobs that survive the prefilter (bounded per run).
- Result: ATS jobs appear in `/jobs`, alerts, and get Auto apply where the Greenhouse/Lever adapters exist (research item 15).

### 3.6 API
- `GET /ats/search?q=&platform=&location=&remote=&posted_within_days=&hide_applied=&page=` → `{groups[], total_companies, total_jobs, registry: {companies, jobs, last_refresh_at}, topup_task_id?}`. Entitlement `ats_discovery`; rate limit `RATE_LIMIT_ATS_SEARCH` (30/minute).
- `GET /ats/companies/{id}/jobs?q=` → the company's open jobs.
- `GET /ats/jobs/{id}` → one job with full description (fetched and cached on demand); `POST /ats/jobs/{id}/track` → creates the canonical `jobs` row so save / hide / apply buttons work.
- `POST /ats/companies` → user adds a company (§3.1.3). `GET /ats/stats` → registry numbers per platform.
- Admin: `GET/PATCH /admin/ats/companies`, `POST /admin/ats/companies/{id}/crawl`, `GET /admin/ats/health` (crawl errors, switched-off slugs, index size).
- `POST /company-ats/fetch` stays as it is.

---

## 4. Steps
1. Migration `0011`: `ats_companies`, `ats_jobs`, Postgres-only `tsv` + GIN (guarded by dialect), catalog row `company_ats`.
2. `AtsPlatform` interface; wrap the 9 existing fetchers; new fetchers Teamtailor, Rippling, Gem, Homerun, BambooHR (recorded fixtures); `ats_*` catalog rows; URL → platform parsers for all; probe; SSRF guard after redirects.
3. Crawler: due-picker, conditional requests, per-host limiter, backoff, upsert, close/delete, switch-off; beat tick.
4. Seed script + seed file + `.\admin ats-seed`; pipeline link harvesting.
5. Search service: normaliser, synonym table + router expansion, Postgres FTS path, SQLite fallback, grouping, filters, result cache.
6. Live top-up task.
7. `company_ats` pipeline source; on-demand description fetch.
8. Routes (user + admin), entitlement, rate limits.
9. Tests:
   - URL → platform parsing, SSRF;
   - crawler: 304 skip, hash skip, upsert, close + delete, 404 switch-off, backoff;
   - search grouping, filters, hide applied, synonym expansion (router faked), Free-tools-only path;
   - top-up task;
   - pipeline source + dedupe with a LinkedIn copy;
   - user-added company flow;
   - **performance check on Postgres** (new CI job with a Postgres service, 300k synthetic rows, p95 < 3 s).
10. CLAUDE.md §6.12, §7.6, §8–§10.

**New env:** `ATS_CRAWL_TICK_SECONDS` (300), `ATS_CRAWL_BATCH` (50), `ATS_HOT_REFRESH_HOURS` (6), `ATS_COLD_REFRESH_HOURS` (24), `ATS_HOST_CONCURRENCY` (2), `ATS_WORKDAY_MAX_JOBS` (400), `ATS_MIN_COMPANIES` (10), `ATS_TOPUP_MAX_COMPANIES` (40), `ATS_TOPUP_SECONDS` (20), `ATS_CLOSED_RETENTION_DAYS` (7), `ATS_MAX_INDEXED_JOBS` (300000), `ATS_MAX_JOBS_PER_COMPANY` (1000), `ATS_INDEX_COUNTRIES` (IN,remote), `RATE_LIMIT_ATS_SEARCH` (30/minute), `RATE_LIMIT_ATS_ADD_COMPANY` (10/hour).

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| Supabase free tier (500 MB) | No full descriptions; snippets only; closed jobs purged; `ATS_MAX_INDEXED_JOBS` cap with admin warning |
| Being rate-limited or blocked by an ATS | Public read APIs only, conditional requests, 2 per host, backoff, tiered refresh (~3,000 companies/day is about 2 requests a minute, spread over many hosts) |
| Workday tenants are large and slow | Page cap per crawl + live `searchText` for big tenants |
| Common Crawl unreliable (504s seen today) | Offline seeding script with retries; never in the request path |
| Search slower on SQLite than Postgres | Speed target is for Postgres; a Postgres CI job proves it; SQLite path is for dev/tests only |
| Same job from LinkedIn and the ATS | Shared `job_key` + existing dedupe rank (LinkedIn copy kept, ATS link still used by Apply) |
| User-added URLs used for SSRF | Existing `_reject_private_targets`, applied after redirects too; probe runs on the worker |

---

## 6. Not in P6
- Embeddings / pgvector rerank (no embedding model in the router until P7; storage cost) — added later if keyword + synonyms are not good enough.
- Darwinbox, Keka, Zoho Recruit, Freshteam (P6.1); Jobvite, iCIMS, JazzHR, Pinpoint (generic page reader, P8).
- Workday / Recruitee apply adapters (P8).
- Screens (F6): Discover page, grouped company cards, filters, registry stats, "Add company".

---

## 7. Decisions (approved by owner, 2026-10-19)
1. **Index 13–14 platforms** (§3.0 tiers A–C, BambooHR if it verifies); India HR suites follow in **P6.1**. *(Revised on owner request: more ATS tools.)*
2. **Keyword search + synonyms, no embeddings in P6.** Postgres full-text with a built-in synonym table and a cached AI expansion; Free tools only uses the table alone.
3. **Short rows, no full descriptions in the index**; full text fetched on demand.
4. **Shared registry**: companies added by any user help everyone; only public job lists are stored.
5. **Grow from pipeline apply links automatically**, plus a ~3,000-company probed seed file.
6. **Index jobs feed normal searches and alerts** as a new `company_ats` source, on by default.
7. **Tiered refresh:** 6 h for companies people search, 24 h for the rest; 404 three times → switched off.
8. **Live top-up when fewer than 10 companies match**, in the background, capped at 40 companies / 20 s.
9. **Add a Postgres job to CI** to prove the < 3 s target.
10. **Per-company cap 1,000 jobs**; giant tenants indexed only for India + remote, the rest searched live.
