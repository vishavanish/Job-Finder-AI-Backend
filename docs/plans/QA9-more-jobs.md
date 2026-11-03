# QA9 — More jobs per search

Status 2026-11-02: owner accepted all ten recommended decisions in §9. **Built (not committed, not deployed): steps A, B, C, D, J** (CLAUDE.md §6.24). Next: owner runs S1–S3 and the §7 script, then measure, then E, F, G, H. "Jobs per source" is on the Jobs page, not in Admin yet.

Owner's request: "when user click job search they get huge number of jobs, currently we have only 2-6 jobs, also many job
tools and libs are failed. Go next step and find alternative and free and best." Also: "now in the AI era and new trending
technique have to find jobs - for free."

## 1. Short answer

**The main problem is not missing sources. We already find about 150 unique jobs per search and throw away about 97 % of them.**

1. **Filters remove most jobs** (measured). In the 4 titles logged, 55 unique jobs came in and 1 was kept.
2. **The 24-hour window hides most jobs** (measured on Indeed). 7 days gives 3–5 times more for narrow titles; 30 days gives 10–14 times more.
3. **Two sources are switched on but give nothing.** Jooble very likely has a key for the wrong country. Careerjet's 403 is still unexplained.
4. **New free sources help, but less than 1 and 2.** The best new one is Himalayas' official free API (158 remote jobs open to India).
5. **"AI-era" methods help with ranking and finding companies.** They don't replace job sources (§6).

**Target:** at least 100 jobs on the first search of the day (today 2–6), with at least 30 of them strong matches. This is an estimate until measured (§8).

## 2. Where jobs are lost (the funnel)

M = measured, C = read in the code (the size of the loss is not measured), G = my guess.

| # | Step | Code | What it does | Likely cost | Type |
|---|---|---|---|---|---|
| 1 | Only the chosen places | Profile locations | Pune + Mumbai only; each extra place adds about 1 min per title | Owner's choice | C |
| 2 | Results per title | `results_per_keyword` = 25 | Indeed loads 100 per request (`jobs_per_page = 100`), then we keep 25. "AI Engineer", Pune, 24 h has 36, so we lost 11 at no saving | Up to 75 per Indeed call, for free | M |
| 3 | Time window | `max_age_hours` = 24 unless Profile "posted within" is set | See the table below | **3–14 times fewer jobs** | M |
| 4 | Remote feeds gated | `_remote_ok` | Run only with a Remote place or the remote work mode | All remote jobs, if off (owner's setting not known) | C |
| 5 | Remote feeds' age filter | `is_recent` in `_run_feed` | RemoteOK: 0 relevant jobs in 24 h, 2 in 7 days, 16 in 30 days. This is why feeds "return 0" | Most feed jobs | M |
| 6 | Feed coverage | `feeds.py` | Himalayas uses the browse feed, not search by country; Jobicy asks for 50 jobs without `geo` | About 150 Himalayas jobs | M |
| 7 | Keyed APIs | `aggregators.py` | Page 1 only. Jooble and Careerjet drop anything outside the window, even though results aren't sorted for it | Jooble/Careerjet: all (see §4) | C |
| 8 | Repeats within a search | `claim_job_key` | 83 % of raw jobs in the 4 logged titles (268 of 323), 57 % in the earlier search (202 of 354). Correct, but similar titles add little | Not a loss; it shows extra similar titles don't help | M |
| 9 | Skills prefilter | `keyword_prefilter_min_pct` = 30, `weighted_keyword_score` | Job must mention 30 % of **all** profile skills, by weight. A long skill list fails most jobs. **Snippet sources (Adzuna, Jooble, Careerjet, and index jobs past the first 10) can hardly reach 30 %** | Big share of "filtered" (split needed, §7) | C |
| 10 | AI minimum score | slider = 70 | Below 70 is removed from the list | Big share of "filtered" | C |
| 11 | Earlier low scorers | `decide` | Skipped until the profile changes or the slider is lowered | 31 % of unique jobs (17 of 55) | M |
| 12 | Already seen | `decide` | Each job shows once; a second search the same day finds little | 9 % here (5 of 55); nearly all on repeat searches (earlier search: 152 unique, 0 new) | M |
| 13 | Career-page index: world first, place later | `_index_rows`: `limit(limit * 6)` | Takes the top 150 rows **worldwide**, then keeps Pune/Mumbai. Most rows are probably from other countries, and that likely explains "0–11 jobs per title" | Possibly most index jobs (count needed, §7) | C |
| 14 | Career-page index: title match | `_tsquery` | Title words only. The pipeline never asks AI for other titles (`expand_with_ai` runs only from the ATS page), so "LLM Application Developer" has no synonyms | Some | C |
| 15 | Career-page index: limit and window | `limit` = 25 per title, 14 days minimum | Hard cap of 25 per title | Some | C |

**What the logs show (4 titles):** 323 raw → 268 repeats → 55 unique → 5 seen, 17 earlier low score, **32 filtered**, **1 kept**.

**Time window, measured on Indeed (JobSpy, up to 100 results, from the owner's PC):**

| Title, place | 24 h | 3 days | 7 days | 30 days |
|---|---|---|---|---|
| AI Engineer, Pune | 36 | 53 | 100+ | 100+ |
| Full Stack Python Developer, Pune | 3 | 5 | 14 | 42 |
| Generative AI Engineer, Mumbai | 4 | 5 | 22 | 55 |

Each call took 1–7 s. LinkedIn takes about 45–60 s per title and place through JobSpy.

**LinkedIn is the fragile one.** About 10 quick page requests from the PC, without descriptions, got a 429 (too many requests). So LinkedIn stays at 25. The test was too broken by 429s to compare time windows.

## 3. Sources: live test results (2026-11-02)

**All tests ran from the owner's home connection, with the app's honest User-Agent. The server's Oracle IP may be treated differently; §7 re-tests the main ones there.**
"Relevant" is a loose check: at least half of the title's words appear in the job title. So these counts are upper limits, and the AI still has to judge the jobs. Searched: the 7 titles × Pune and Mumbai.

### Open to use

| Source | Result today | Limits / terms | Verdict |
|---|---|---|---|
| **Himalayas search API** `himalayas.app/jobs/api/search?q=&country=India` | **158 unique relevant** (2 pages): 59 within 7 days, 60 more within 30 days, 39 older. All remote | Official, free, no key; free key gives 60/min and 10,000/day. Their rules ban scraping the website, not this API | ✅ **Best new source** |
| **Workable job search** `jobs.workable.com/api/v1/jobs` | **46 unique in India**: 1 in 24 h, 7 more in 7 days, 9 more in 30 days, 29 older. 1 timeout in 28 calls | No key. **Not a documented API**, so it can change. robots.txt doesn't block it; a keyword check of their terms found no scraping clause (not read in full) | ✅ Add, low priority |
| **Jobicy** with `geo=apac` | 25 relevant within 7 days (5 in 24 h) | Free, asks for credit and a link to the source | ✅ Small fix |
| **RemoteOK / Remotive** | 2 relevant each in 7 days; 16 and 6 in 30 days | Already built, link-back required | Keep; helped only by a wider window |
| **Amazon Jobs** `search.json` | 13 relevant in India; place filter ignored (Pune returned Bengaluru) | Undocumented | ⏸ Too small |
| **Hasjob** feed (India) | Feed answers (54 kB); jobs not counted | Not checked | ⏸ Untested |

### Keys we hold

| Source | Result | Verdict |
|---|---|---|
| **Adzuna** | Works (17 for AI Engineer, Pune; 0–1 for narrow titles, from the earlier test). We send 1-day age and page 1 | ✅ Wider window, up to 50 per page. Not re-tested today |
| **Jooble** | See §4 | Fix the key |
| **Careerjet** | See §4 | Diagnose |
| **SerpApi / JSearch** (Google Jobs, which includes Indian boards) | **Untested** (keys are only on the server). They don't run on platform keys in production unless an admin gives the `shared_search_keys` entitlement. Quotas are small: about 100 and 200 calls a month, from our 2026-10-16 research | Owner's account only (Decision 9); test after giving the entitlement. §7 does not test them |

### Answer today, but their terms ban robots

| Source | Result | Terms |
|---|---|---|
| Talent.com | 170 unique relevant; page includes full job data | Bans automated scraping. Has a publisher programme (sign-in page seen; **untested**) |
| Shine | 128 unique relevant; "13,550" AI Engineer jobs in Pune by its own page title | Bans page-scrape, robots, crawlers |
| Cutshort | Sitemap: 44,816 jobs, 10,122 with Pune/Mumbai in the link; job pages carry dates | Bans bots. API is "at its discretion" for eligible customers |
| Hirist | Sitemap: 29,427 job links; pages load data in the browser | Bans crawling |
| Internshala | 21 unique relevant, mostly junior | Bans scraping, spiders and bots |
| Glassdoor | Page answered: "2,711" AI Engineer jobs in Pune, with some postings 193 days old. JobSpy got 403 earlier | Terms page refused us (403); not read |
| Apna | Page answered; my count script failed, so **no count** | Terms not found |

**Recommendation: don't scrape these from the server.** Getting blocked would also put the server's IP at risk, and LinkedIn and Indeed run from that same IP. The allowed route is the browser extension: add these sites to it so a user can save jobs from pages they open themselves (Decision 6).

### Not usable

| Source | Result |
|---|---|
| Foundit, Instahyre, SimplyHired, WhatJobs site | 403 |
| WhatJobs API | Needs a publisher id (free signup not tested) |
| Hiring.cafe | Cloudflare challenge; we don't work around it |
| TimesJobs | Connection failed |
| Greenhouse `my.greenhouse.io` | Needs sign-in |
| SmartRecruiters global search | Answers with 40,982 jobs, but my place filters were ignored (French jobs first) |
| jobdataapi.com | Needs a key to page through results |
| apijobs.dev, Microsoft Careers | SSL error from here (untested) |
| NCS (government) | No public jobs API found |
| Naukri | Captcha (earlier test); stays email + extension only |

**Alternatives to JobSpy:** `ts-jobspy` and `python-jobscrape` are ports or forks of the same approach and add no new Indian source. JobFunnel was reportedly archived in December 2025. I did not install these. **Keep JobSpy; a new library won't fix this.** It works. The limits come from how we call it.

## 4. Why Jooble returns 0 and Careerjet returns 403

**Jooble — likely cause found, not yet confirmed.** Jooble's help page says each country needs its own key, and a key made on jooble.org gives **US jobs only**. Our code calls `jooble.org/api/{key}` with "Pune". That fits "key accepted, 0 results". A second problem: we also drop results older than the window, and we don't ask Jooble for that window. I can't test the real key here because it is in the protected `.env`; §7 does.
**Fix:** create a key at the India portal (in.jooble.org, free), add a host setting, and stop dropping results outside a short window.

**Careerjet — cause not known yet.** The docs give one 403 reason: missing `user_ip` or `user_agent`. Our code always sends both, falling back to `127.0.0.1`. From the PC, a fake key gets 401 "Invalid API key", so a 403 suggests the server's key passes that check and something else is refused. Possible causes: the `127.0.0.1` address, or the server's Oracle IP. **We never see the reason, because `_call` throws away the response text.** §7 prints it (never the key).

## 5. Options ranked by jobs gained per effort

Job counts are estimates unless marked measured. Each is the extra jobs **shown** per first search of the day.

### Settings only (no code; owner can try today)

| # | Change | Where | Extra jobs | Cost |
|---|---|---|---|---|
| S1 | Posted within = **168 hours (7 days)** | Profile | Largest: 3–5× raw on Indeed (measured) | Slightly older jobs |
| S2 | Lower the score slider to **50** | Jobs page | Brings back earlier low scorers at once, no AI call | Weaker matches mixed in, until Step A |
| S3 | Add **Remote** to work modes (not to places) | Profile | Turns on remote feeds with no extra LinkedIn/Indeed time | Remote jobs mixed with city jobs |
| S4 | Keep **fewer, more different** titles and only real skills | Keywords & skills | Fewer jobs fail the 30 % rule; fewer repeats | Owner's editing time |
| S5 | `ATS_SEARCH_MIN_DAYS=30` | Server `.env` | More career-page jobs | Older career-page jobs |

Doing S1–S3 first is free and tells us how much Steps A–C still need to do.

### Code (best first)

| Step | Change | Extra jobs (estimate) | Effort | New risk |
|---|---|---|---|---|
| **A** | **Fix the prefilter + two result groups.** A job passes if its title fits **or** skills fit; snippet jobs are judged on title. Jobs at 70+ show as "Strong matches", 50–69 as "Possible matches", instead of disappearing | **+50–150**, the biggest gain | Medium (backend + Jobs page) | More AI calls, see Step H |
| **B** | **Indeed takes 100 per title** (own JobSpy call). LinkedIn stays at 25 | +50–150 raw at 0 extra Indeed requests up to 100 (measured) | Small | Blocking by Indeed unlikely but not ruled out; health check watches it |
| **C** | **Default window 7 days** where Profile is empty; feeds included | Same as S1, for every user | Small | Older jobs; the "posted" date is shown on cards |
| **D** | **Fix the career-page index.** Filter by place **in the database, before the limit**; limit 100; use AI title synonyms in search | +20–100 (needs §7 count) | Small | None outside; a slower query needs checking |
| **E** | **Himalayas search + Jobicy `geo`** | +30–80 (remote; 158 relevant before scoring, measured) | Small | Free key advised |
| **F** | **Fix keyed APIs.** Jooble India host + key, keep Careerjet's error text, Adzuna up to 50 per page | +10–60, if §7 confirms Jooble | Small | Daily cap: 60 calls/user may need a raise |
| **G** | **"Seen earlier" group.** Repeat searches also show earlier jobs, marked, below new ones | Repeat search no longer looks empty | Small (frontend + API) | Changes QA8 decision 5 |
| **H** | **Keep AI costs free.** Rank locally, AI-score only the top N per search (default 150). The rest appear marked "not AI-checked" | Allows volume without running out of free quota | Medium | Some jobs lack an AI score |
| **I** | **Workable search** | +5–20 | Small | Undocumented endpoint |
| **J** | **Per-source counts** on each search, in Admin | Proof of what works | Small | None |
| **K** | **Extension: add Shine, Cutshort, Hirist, Talent.com** | Depends on what the user opens | Small | None on the server |

**Suggested order:** S1–S3 → A, B, C, D, J → measure → E, F, G, H → measure → I, K, then §6.

**Not proposed:** more LinkedIn results, faster LinkedIn pacing, proxies, disguised requests, or working around captchas or Cloudflare (CLAUDE.md §12).

## 6. AI-era techniques (free tiers)

None of these were tested with real keys. Free limits come from third-party articles that disagree, so check each provider's page before building.

| Technique | What it does for us | Free limit reported | Verdict |
|---|---|---|---|
| **AI title suggestions** (already built: `expand_with_ai`, `title_suggest`) | Finds different job names ("RAG Engineer", "Prompt Engineer"). Similar titles only give repeats (83 %), so different ones matter | Our current AI models | ✅ **Do first**; included in Step D, plus a hint on the Keywords page |
| **Semantic matching** (small local model, e.g. all-MiniLM-L6-v2) | Ranks by meaning, not exact skill words; could replace the skills prefilter and choose Step H's top N. Recent research uses this with keyword search | Free, runs on our server | ✅ **Best next step**, after checking server memory. Not tested on the server's ARM CPU |
| **AI web search to find companies** (Tavily, Exa) | Once a day per title, search `site:jobs.lever.co "AI engineer" Pune` and similar, then pass links to the existing `ats_registry.harvest`, which grows the index. Results are shared, so calls stay few | Tavily about 1,000 searches/month; Exa about $10/month credit | ✅ Good trial, later. Check each API's terms first |
| **Gemini with Google Search** | Same discovery idea | Sources conflict (500/day vs none without billing) | ⏸ Check first |
| **AI page reader** (Crawl4AI, or a model reading HTML) | Reads career pages on no known platform, which the resolver already finds | Free library; model calls cost quota | ⏸ Later, for high-value companies only |
| **MCP servers** (Himalayas has one) | Lets AI assistants call a source. Our server can call the same API directly | — | ❌ No benefit here |

**Limit:** these tools can't legally reach sites that block us. Using an AI "reader" or proxy to get past a block is still bypassing it, and we won't do that.

## 7. Numbers needed from the server (read-only)

This changes nothing and never prints a key. It uses a few Jooble, Careerjet and Adzuna calls from the daily quota. Paste at the server's shell prompt:

```bash
cd /var/www/job_finder && source venv/bin/activate && python - <<'EOF' 2>&1 | grep -v "^INFO"
import requests
from sqlalchemy import text
from app.core.db import SessionLocal
from app.services import ats_search, ats_registry, key_store
db = SessionLocal()
print("== A. prefilter vs AI, why jobs were filtered (all users)")
for row in db.execute(text("""select state, llm_score is null as no_ai,
    case when llm_score is null then null when llm_score < 50 then '<50' when llm_score < 70 then '50-69' else '70+' end as band,
    count(*) from user_jobs where updated_at > now() - interval '7 days' group by 1,2,3 order by 1,2,3""")):
    print(tuple(row))
print("== B. settings of each search profile")
for row in db.execute(text("select coalesce(jsonb_array_length(skills::jsonb),0), coalesce(jsonb_array_length(titles::jsonb),0) from search_profiles")):
    print("skills, titles:", tuple(row))
for row in db.execute(text("select posted_within_hours, locations, work_modes from job_preferences")):
    print("prefs:", tuple(row))
print("== C. career-page index: real matches for each title")
enabled = ats_registry.enabled_platforms(db)
for title in ["LLM Application Developer", "AI Engineer", "Senior Backend Developer", "Full Stack Python Developer",
              "Generative AI Engineer", "Applied AI Engineer", "AI Solutions Engineer"]:
    phrases, _ = ats_search.expansions(title)
    out = []
    for place, days in ((None, 14), (["pune", "mumbai"], 14), (["pune", "mumbai"], 30), (["india"], 30)):
        q = ats_search._match_query(db, phrases)
        out.append(ats_search._filtered(db, q, platforms_=None, location=place, remote=None,
                                        posted_within_days=days, enabled=enabled).count())
    print(title, "world14d, city14d, city30d, india30d =", out)
def show(name, resp):
    print(name, resp.status_code, resp.text[:300].replace("\n", " "))
UA = {"User-Agent": "Mozilla/5.0 (compatible; JobFinderAI/1.0)"}
print("== D. Jooble")
k = key_store.resolve(db, user_id=None, scope="jooble")
if k:
    for host in ("jooble.org", "in.jooble.org"):
        for loc in ("Pune", "India"):
            r = requests.post(f"https://{host}/api/{k.value}", json={"keywords": "AI Engineer", "location": loc}, headers=UA, timeout=30)
            try:
                d = r.json(); print(host, loc, r.status_code, "total", d.get("totalCount"), "got", len(d.get("jobs") or []), "first", [j.get("location") for j in (d.get("jobs") or [])[:2]], "newest", max([j.get("updated", "") for j in d.get("jobs") or []] or [""]))
            except ValueError:
                print(host, loc, r.status_code, "not JSON")
print("== E. Careerjet")
k = key_store.resolve(db, user_id=None, scope="careerjet")
if k:
    for ip in ("127.0.0.1", "49.36.0.1"):
        r = requests.get("https://search.api.careerjet.net/v4/query", auth=(k.value, ""), headers=UA, timeout=30,
                         params={"locale_code": "en_IN", "keywords": "AI Engineer", "location": "Pune", "user_ip": ip, "user_agent": "Mozilla/5.0"})
        print("user_ip", ip, r.status_code, r.text[:300].replace("\n", " ") if r.status_code != 200 else "jobs " + str(len(r.json().get("jobs") or [])))
print("== F. Adzuna 1 vs 7 days")
k = key_store.resolve(db, user_id=None, scope="adzuna")
if k:
    a, _, b = k.value.partition(":")
    for days in (1, 7, 30):
        r = requests.get("https://api.adzuna.com/v1/api/jobs/in/search/1", headers=UA, timeout=30, params={"app_id": a, "app_key": b, "what": "AI Engineer", "where": "Pune", "max_days_old": days, "results_per_page": 50})
        print(days, "days", r.status_code, r.json().get("count") if r.status_code == 200 else "")
print("== G. open sources from the server's IP")
r = requests.get("https://himalayas.app/jobs/api/search", params={"q": "AI Engineer", "country": "India"}, headers=UA, timeout=30); print("himalayas", r.status_code, len(r.json().get("jobs", [])) if r.ok else "")
r = requests.get("https://jobs.workable.com/api/v1/jobs", params={"query": "AI Engineer", "location": "Pune, India"}, headers=UA, timeout=30); print("workable", r.status_code, r.json().get("totalSize") if r.ok else "")
r = requests.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 100, "geo": "apac"}, headers=UA, timeout=30); print("jobicy", r.status_code, len(r.json().get("jobs", [])) if r.ok else "")
from jobspy import scrape_jobs
for h in (24, 168):
    print("indeed AI Engineer Pune", h, "h:", len(scrape_jobs(site_name=["indeed"], search_term="AI Engineer", location="Pune", results_wanted=100, hours_old=h, country_indeed="India", verbose=0)))
db.rollback(); db.close()
EOF
```

I have not run this script, because it needs the server. Table names are checked against the code. If one section fails, send me the error anyway, because the other sections still matter. Also send:

```bash
free -h && nproc && git -C /var/www/job_finder log --oneline -1
```

**Blank spaces this fills in:** how much "filtered" is prefilter vs AI score (A), whether Step D is worth it (C), Jooble's and Careerjet's real cause (D, E), whether the local semantic model fits (memory), and whether `bd19c2a` is deployed.

## 8. Target and how it is measured

| Measure | Today | Target after A–D + J | After E–H |
|---|---|---|---|
| Unique jobs per search | about 150 (measured) | 300+ | 500+ |
| Jobs shown, first search of the day | 2–6 (owner) | **100+** | **200+** |
| Strong matches (70+) | 2–6 | 30+ | 50+ |
| Repeat search, same day | 0–4 | 10+ new, plus earlier jobs marked | Same |
| Search time | about 6 min | Not slower than today | Not slower |
| Sources giving 1+ job | LinkedIn, Indeed, Adzuna, index, extension | Same + more from the index | + Himalayas, Jooble, Jobicy, feeds |

These targets are estimates, based on the same 7 titles, Pune + Mumbai.
**Honest limit:** with 2 cities and narrow AI titles, the real market is limited. "Huge" means hundreds, not thousands. More cities or India-wide would add the most after that. Quality also matters: many "possible matches" will be weaker fits, which is why they're in their own group.

**How we measure:** run the same search before and after each group, with "show seen" on so old jobs don't hide the difference. Read `counts` on `GET /pipeline/{id}` and the per-source counts from Step J. The two groups let us check quality too: sample 20 "possible" jobs and see if they are worth applying to.

**Tests:** recorded fixtures in `tests/fixtures/sources/` for new adapters; prefilter and two-group rules in the fake pipeline (like `tests/test_qa8_search_speed.py`); index place filter in the `ats-postgres` CI job; Jobs page groups in Vitest. Then update CLAUDE.md (§6.x, §7.4 catalog).

**Main files:**
- `app/services/job_sources.py` (B)
- `app/core/celery_tasks.py` (A, H)
- `app/services/search_profiles.py` (A)
- `app/services/ats_search.py` (D)
- `app/providers/job_sources/feeds.py`, `aggregators.py`, `base.py`, `registry.py` (E, F, I)
- `app/models/schemas.py` (C)
- `job-finder-frontend/src/features/jobs/JobsPage.tsx` (A, G)
- `job-finder-frontend/extension/` (K)
- a migration for new catalog rows (E, I)

## 9. Decisions for the owner (recommended defaults)

1. **Try the settings first (S1–S3), then run the §7 script and send me the output.** _Recommended: yes, today. It costs nothing and sharpens every number here._
2. **Build Steps A, B, C, D, J first, then measure.** _Recommended: yes._
3. **Weak matches:** show 50–69 in a "Possible matches" group rather than hiding them. _Recommended: yes. Default slider stays 70 for "Strong"._
4. **Default "posted within":** 7 days, not 24 hours, when Profile is empty. _Recommended: 7 days._
5. **Repeat searches:** show earlier jobs in a marked "Seen earlier" group. This changes QA8 decision 5. _Recommended: yes._
6. **Sites that ban robots (Shine, Talent.com, Cutshort, Hirist, Internshala):** no server scraping; add them to the browser extension, and ask Talent.com and Cutshort about partner access. _Recommended: yes._
7. **AI scoring cap:** AI-score the best 150 jobs per search; the rest show as "not AI-checked". _Recommended: yes, to stay within free AI quotas._
8. **Jooble:** you create a free key on Jooble's India portal; I add the India host. Your key at the moment may be US-only. _Recommended: yes, after §7 confirms it._
9. **Google Jobs (SerpApi/JSearch):** owner's account only through `shared_search_keys`, because the free quotas are too small to share. _Recommended: yes, as a test._
10. **AI-era next steps:** after the targets are measured, trial local semantic matching first, then Tavily/Exa company discovery. _Recommended: yes; nothing paid._

## Sources

- [Jooble REST API docs (country keys)](https://jooblehelpcenter.freshdesk.com/en/support/solutions/articles/60001448238-rest-api-documentation)
- [Careerjet API docs (403 meaning)](https://www.careerjet.im/partners/api)
- [JobsPipe on Careerjet](https://jobspipe.dev/blog/careerjet-api.md)
- [Himalayas API](https://himalayas.app/api)
- [Scrapfly: open source job scrapers](https://scrapfly.io/blog/posts/best-open-source-job-scrapers)
- [ts-jobspy](https://awesome.ecosyste.ms/projects/github.com%2Falpharomercoma%2Fts-jobspy)
- [python-jobscrape](https://pypi.org/project/python-jobscrape/)
- [Firecrawl: free web search APIs](https://www.firecrawl.dev/blog/best-free-web-search-apis)
- [Tavily vs Brave vs Exa](https://dev.to/build996/tavily-vs-brave-vs-exa-free-search-apis-for-ai-agents-59m1)
- [Gemini grounding free quota thread](https://discuss.ai.google.dev/t/grounding-free-quota-clarifying-2026-rules-for-gemini-2-5-flash-paid-tier-1/144593)
- [JobMatchAI, ACL 2026 demo (hybrid search + all-MiniLM-L6-v2)](https://aclanthology.org/2026.acl-demo.52.pdf)
- [Dense embeddings for job matching](https://arxiv.org/pdf/2609.23307)
