# QA3 — Company career pages inside Job Search (no separate ATS step)

Status: **approved and built** (2026-10-25, recommended defaults; decision 5 set by the owner). Section E
was then superseded by QA4 (the page became Target companies). See CLAUDE.md §6.16.

## 1. The owner's question

> Why does the user handle the ATS page separately? When the user clicks Job search, ATS jobs should also be
> picked up dynamically, without manual handling.

Short answer: Job Search **already** asks the ATS index (source `company_ats`, "Company career pages"), but three
things make it look useless, so people end up on the ATS Targets page and add companies by hand:

| # | Cause (found in code + read-only production numbers, 2026-10-25) | Effect |
|---|---|---|
| 1 | The index lookup uses the search's age window. Default `max_age_hours` is **24** → `posted_within_days=1` (`ats_search.index_jobs`). | Production index: **128,091 open jobs**, but only **246** posted/first-seen in the last day. A normal search gets almost nothing from company sites. The ATS page defaults to 90 days, so it "works" there. |
| 2 | The live top-up (`ats_search.topup`: Workday/SmartRecruiters text search + refresh of stale companies) runs only from the ATS page (`GET /ats/search`). | When the index is thin for a title, Job Search never looks live. |
| 3 | The registry barely grows by itself: 2,087 companies, **2,064 from the seed file**, only **23** learnt from searches (`harvest` only sees apply links that already point at an ATS host; LinkedIn/Indeed/Naukri jobs mostly don't). Seed is mostly foreign start-ups; only 8 Lever companies. | Indian companies the user actually sees on LinkedIn/Naukri are missing, so users must "Add company" by hand. |

Plus: companies with slug names ("Openai", "Wk", "Bjakcareer") — the crawler copies the board's real name for only
6 of 12 platforms.

## 2. What other products do (research, Oct 2026)

- **Read each ATS's public board API and normalise** — what we do. Paid aggregators on Apify claim 10k–175k company
  boards; Workday/iCIMS/Taleo are the hard ones. Price ≈ $1–3.70 per 1,000 jobs.
- **ATS detection from a company name or website** ("ATS detector" tools): (a) fingerprint the company's careers
  page for links/embeds (`boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `*.myworkdayjobs.com`,
  `smartrecruiters.com`, `*.recruitee.com`…), confirm against the platform's public API; (b) when no site is known,
  probe likely slugs on each platform and check the board's name matches. **This is the missing piece.**
- Search-engine "site:" discovery (e.g. Brave API): the free tier is gone for new accounts (monthly credit + card).
  Not recommended.

## 3. Proposed logic — "learn companies from every search"

```
Job Search (keywords, places)
 ├─ other sources (LinkedIn, Indeed, Naukri, feeds…)  ──► company names (+ website when the board gives one)
 │                                                          │
 │                                                          ▼
 │                                     (A) Company resolver (background, maintenance queue)
 │                                         careers-page fingerprint → slug probe → name check
 │                                         new company → registry → crawled at once
 ├─ company_ats: index lookup  (B) wider window, newest first
 └─ (C) thin index? → live top-up in background, jobs appended to the same run's feed
```

### (A) Company resolver — new `services/ats_resolver.py`
- Input: company names (and `company_url` when JobSpy/feeds return one) from every pipeline run, after dedupe.
  Skip names already in the registry or in the probe cache.
- Step 1 — **website fingerprint** (only when a company URL is known): fetch the homepage and `/careers`,
  `/jobs` (SSRF guard `_reject_private_targets`, 5 s, ≤ 300 kB, no cross-host redirects except to a known ATS host),
  collect links/iframes/scripts on any supported ATS host → `platforms.parse_url`. Catches Workday, which cannot be
  guessed.
- Step 2 — **slug probe**: candidates from the name (`Acme Technologies Pvt Ltd` → `acme`, `acmetechnologies`,
  `acme-technologies`; ≤ 3), tried on the guessable platforms (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee,
  Workable, Breezy, Teamtailor) with one cheap call each, using the existing host slots.
- **Name check** before adding: the board's own name (or the company name on its jobs) must match the searched
  name (normalised, suffixes removed, token overlap ≥ 0.8) — stops "apple" on Greenhouse being some other Apple.
  A website hit counts as verified.
- Result stored in a new table `ats_probe_cache` (name_norm unique, result platform/slug or none, checked_at);
  misses retried after 30 days. New companies get origin `resolver` and are due for a crawl at once.
- Budget: ≤ 30 new names per run, ≤ 8 at a time, whole task ≤ 60 s; never delays the search.

### (B) Index window for company jobs
- Company jobs in a Job Search use `max(search window, ATS_SEARCH_MIN_DAYS)` (default **14 days**), by
  `coalesce(posted_at, first_seen_at)`, newest first. Board posts are fresh only for a day, but a career-page post
  from last week is usually still open (the crawler closes removed ones).
- Cards still show the real "posted N days ago".

### (C) Live top-up inside Job Search
- If the index gives fewer than `ATS_MIN_COMPANIES` (10) jobs for a keyword, queue the existing `ats_topup` for it
  (the same 10-minute lock per query). When it finishes, rerun the index lookup and **append** the new jobs to the
  same run (the feed already takes jobs while scoring). The Activity tray shows "Company career pages: checking N
  companies live".

### (D) Real company names (the earlier item 2, folded in)
- Every platform's `list_jobs` returns the board/company name (Ashby `organization`, SmartRecruiters
  `company.name`, Workday site title, Lever/Gem/Rippling/Breezy from their board endpoints). Crawler keeps
  admin renames (existing rule). One-off backfill: next crawl of each company fixes it — no script needed.
- The resolver names a company by the name it was found with (LinkedIn's display name) when the board has none.

### (E) Job Search does the work; the company page is optional (owner, 2026-10-25)
- **Jobs** is where everyone searches. Company career-page jobs come in with the normal results with a
  "Company site · Greenhouse" badge, and Jobs gets a filter "Only company career pages". Nobody has to open another
  page for them.
- The **ATS Targets page stays, as an optional extra** for people who want to browse by company:
  - renamed **"Browse companies"** (route `/ats-targets` kept, old links still work);
  - moved out of the main "Workspace" menu group to the bottom of "Search setup", with the hint "Optional";
  - a line at the top of the page: "Job search already includes these jobs — use this page only to browse by
    company";
  - Jobs gets a small "Browse by company" link that opens the page with the current keyword
    (`/ats-targets?q=<keyword>`), so it needs no typing;
  - "Add company" stays on that page as an extra; the resolver makes it unnecessary for most companies.
- Admin › ATS registry is unchanged.

## 4. Decisions (recommended default in bold)

1. Index window for company jobs in Job Search: **14 days minimum** / follow the search window / 30 days.
2. Live top-up during Job Search: **yes, in background, appended to the run** / no.
3. Resolver: **on for every search, ≤ 30 new names per run** / admin-only switch / off.
4. Fetch company websites for fingerprinting (when the board gives a URL): **yes** / slug probe only.
5. UI: **decided by the owner — keep the page as an optional "Browse companies" page (lower in the menu, linked
   from Jobs); Job Search alone brings the career-page jobs.**
6. Paid aggregator (Apify career-site actors) as an extra source: **not now** (can be added later as an optional
   keyed source like Naukri Apify).

## 5. Build order (each its own commit on `dev`)

1. **B + C** — window + live top-up in the pipeline (small, biggest win). Tests: index job 5 days old found by a
   24 h search; top-up queued once when thin and appended.
2. **D** — board names for all 12 platforms (fixtures already recorded). Tests per platform.
3. **A** — resolver + migration `0016_ats_probe_cache` + Celery task on maintenance queue, chained after each run.
   Tests: fingerprint HTML fixtures, slug probe with recorded 404/200, name-check rejects a wrong company, cache
   skip, SSRF block, budget.
4. **E** — frontend: badge + "Only company career pages" filter on Jobs, "Browse by company" link, page renamed
   "Browse companies" and moved lower in the menu with the "Optional" hint and the explainer line (`npm run check`).
5. Blueprint `CLAUDE.md` §6.16 + env docs.

New env (optional): `ATS_SEARCH_MIN_DAYS` (14), `ATS_RESOLVER_MAX_NAMES` (30), `ATS_RESOLVER_SECONDS` (60),
`ATS_RESOLVER_RETRY_DAYS` (30), `ATS_RESOLVER_ENABLED` (true).

## 6. Risks
- Slug probing makes many small requests to ATS APIs: capped per run, host slots, 30-day miss cache.
- Wrong-company matches: name check + website verification; admins can switch a company off (existing).
- Supabase size: the index cap `ATS_MAX_INDEXED_JOBS` (300k) still applies; resolver-added companies are crawled
  with India/remote first like the rest.
