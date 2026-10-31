# QA7 — More job sources

Status 2026-10-31: decision 7 (Keka + Zoho Recruit) approved and built (CLAUDE.md §6.21); decisions 1–6 waiting for the owner.

Owner asked 2026-10-31: is Glassdoor used, and which similar sources are missing? Groups A–C below were
live-tested from the owner's PC on 2026-10-31 (python-jobspy as installed, plain HTTPS clients).

## 1. Test results

| Group | Source | Result | Verdict |
|---|---|---|---|
| A (JobSpy, like Glassdoor) | ZipRecruiter (US + India) | 403 `forbidden aa` (Cloudflare), 0 jobs | ❌ blocked |
| A | Bayt (UAE + India) | 403 Forbidden, 0 jobs | ❌ blocked |
| A | **Glassdoor** (India) — already in the registry, off by default | 403, "location not parsed", 0 jobs | ❌ blocked today (keep off) |
| B (free key) | USAJobs | 401 without key (endpoint alive) | ✅ works with a free key — US government jobs only |
| B | Reed | 401 without key | ✅ works with a free key — UK only |
| B | Findwork.dev | 401 without key | ✅ works with a free key — tech jobs, mostly US/EU/remote |
| C (no key) | **Working Nomads** `workingnomads.com/api/exposed_jobs/` | 200, 37 jobs (9 Python), fields title / company / location / description / tags / pub_date / url | ✅ small remote feed |
| C | **HN job posts** (YC startups) `hacker-news.firebaseio.com/v0/jobstories.json` | 200, 31 posts; title + link only (13 to ycombinator.com) | ⚠️ tiny; useful mainly as company links |
| C | **Landing.jobs** `landing.jobs/api/v1/jobs` | 200, 56 jobs in total (Europe, 5 remote), no company field | ⚠️ tiny, EU only |
| C | Bundesagentur für Arbeit (Germany) | 403 with the documented public key | ❌ |

These were tested from a home IP in India; the OCI server's IP may be treated differently (Glassdoor,
ZipRecruiter and Bayt could be re-checked from there, read-only, once).

## 2. Proposed changes (decisions for the owner)

1. **Add Working Nomads** as a remote feed (same code path as RemoteOK/Remotive: cached hourly, keyword +
   region + age filters, runs only on remote searches). _Recommended: yes._
2. **Add HN job posts** to the existing `hn_hiring` source (same "HN" badge), and send each post's link to
   `ats_registry.harvest` so YC companies on Ashby/Greenhouse/Lever join the company-career-pages index.
   _Recommended: yes (cheap; the harvest is the real value)._
3. **Landing.jobs**: skip for now (56 EU jobs, no company name). _Recommended: skip._
4. **USAJobs / Reed / Findwork.dev** as keyed sources (user's own free key, off by default, like Adzuna).
   None covers India. _Recommended: only Findwork.dev (tech/remote); skip USAJobs and Reed unless you
   target the US/UK._
5. **ZipRecruiter / Bayt**: don't add (blocked). **Glassdoor**: keep off; show "blocked by the site" in its
   health when it keeps returning 403.
6. **Next, higher value for India:** test Group E — Darwinbox, Keka, Zoho Recruit, Freshteam career pages
   (P6.1) — before building more feeds. _Recommended: yes._

## 2b. Group E — Indian ATS career pages (tested 2026-10-31, app's own User-Agent, no disguise)

Where they show up: 800 Indian Indeed jobs had 479 company apply links — Workday 53, Oracle 23,
PeopleStrong 18, Workline 14, RippleHire 9, Keka 6, Darwinbox 6, Zoho Recruit 6, HireWand 5, Freshteam 0.
Companies per platform in one Common Crawl index (CC-MAIN-2026-43): Keka 182, Zoho Recruit 160
(.com 147 + .in 13), PeopleStrong 65, Darwinbox 27 (Airtel, 1mg, BharatPe, Brigade…).

| Platform | Public feed | Result | Verdict |
|---|---|---|---|
| **Keka** | `GET https://{co}.keka.com/careers/api/jobs/default/active` (JSON; company name from `/careers/api/organization/default/careerportalinfo`) | 5/5 companies: 90, 30, 17, 12, 3 jobs; full description, city + country code, `publishedOn`, skills, experience, salary range | ✅ add — best quality |
| **Zoho Recruit** | `GET https://{co}.zohorecruit.{com,in}/jobs/Careers` — the public page embeds the job list as JSON | 5/5 companies: 101, 67, 25, 11, 2 jobs; title, city, state, country, remote flag, job type; description + date on most | ✅ add — page is ~1.7 MB, so crawl daily; job link `/jobs/Careers/{id}` |
| Darwinbox | `{co}.darwinbox.in/ms/candidatev2/main/careers/allJobs` | Non-browser clients get a ~1 kB placeholder page; the real app is only served to browsers | ❌ skip (we don't disguise requests) |
| PeopleStrong | `{co}careers.peoplestrong.com/job/detail/…` | Client-rendered app (same 56 kB shell for every job); API not found yet | ⏸ investigate later (L&T, Amara Raja use it) |
| RippleHire | per-job pages behind a token, with JSON-LD `JobPosting` | No company-wide list found | ⏸ later |
| Workline, HireWand | — | No sample links in the second scan | ⏸ later |
| Freshteam | — | 0 of 800 Indian jobs | ❌ skip |

**Decision 7 — add Keka and Zoho Recruit as ATS platforms 13 and 14** in `providers/ats/platforms.py`
(`parse_url`, `list_jobs`, `job_detail`, `board_name`), so they join the company-career-pages index, Job Search
(`company_ats` source), Target companies and the resolver. Seed: Common Crawl (several indexes, ~350+
companies from one), then grown automatically from apply links in search results (`ats_registry.harvest`).
Estimated volume: ~5k Keka + ~6k Zoho jobs, mostly India. _Recommended: yes — do this before decisions 1–4._

## 3. Build notes (if approved)
- `providers/job_sources/feeds.py`: `working_nomads` feed; registry row `SourceSpec("working_nomads",
  "Working Nomads", "feed")`; migration adds the `provider_catalog` row (free, no key, on for remote).
- `hn_hiring`: also read `jobstories` (title → company/role/location parsed like the monthly thread headers;
  link kept as `apply_url`), harvest links.
- Findwork.dev: `aggregators.py` keyword source, key scope `findwork`, daily caps like Adzuna.
- Recorded fixtures in `tests/fixtures/sources/`; CLAUDE.md §6.7 table + a §6.21 section.
