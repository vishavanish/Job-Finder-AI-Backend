# QA8 — Search speed, fairness between users, and fewer jobs than expected

Status 2026-11-01: owner approved steps A, C and D; **those are built** (CLAUDE.md §6.23), with N = 3. B waits for the server facts (§4); E and F come after measuring. Correction to cause 6: most `HostBusy` failures came from the live check itself (8 parallel checks against hosts allowing 2, failing at once), not only from the crawler.

Owner's observation: earlier versions loaded more jobs, the current one loads fewer, takes a long time to show
them, and users wait when several search at once.

## 1. What the log shows (one real search, 2026-11-01 08:15 UTC, 7 titles, Pune + Mumbai)

| Measured | Value | Where it comes from |
|---|---|---|
| Whole search | **6 min 5 s** (08:15:16 → 08:21:21) | `dispatch_pipeline_group` → last `search_and_score_keyword` |
| First title finished | **2 min 26 s** after the start (08:17:42) | Jobs are scored only after ALL places of a title are scraped |
| One LinkedIn + Indeed scrape, one place | 48–64 s | `searching …` → next source's line |
| Fixed pause between places | 16–17 s per title | `search_delay_sec` |
| Last titles started scraping | **about 3 min after being queued** (08:18:25, 08:18:27) | Waiting for a free worker slot |
| Background crawls in the same slots | 36.7 s, 67.9 s and 68 s each among them; `ats_tick` queues up to 50 companies every 5 min | `crawl_ats_company` |
| Results | 354 found → 202 repeats within the run → 152 unique → **0 new** | Task results |
| Same search, "show seen" on | 4 jobs | Run in the owner's Chrome |
| Live career-page check | 17–19 of 20 companies failed per title (`HostBusy`) | `ats top-up` lines |
| Keyed APIs | Adzuna bad key format, Careerjet 403, Jooble at its daily cap | Worker log |

## 2. Causes

### Slow, and users block each other
1. **Search and background work share one worker pool.** One systemd unit serves `default`, `apply`, `maintenance`
   and `email` (decision of 2026-10-21). Company crawls, Gmail sync and browser applies take the same slots as
   searches. The registry grew by 373 companies on 2026-10-31, including very large Oracle sites, so this got worse
   recently.
2. **No fairness.** A search queues one task per title, first come first served. One user's 7 titles fill the queue,
   and a second user's search starts only after those (each takes 2–4 min).
3. **Nothing shows until a title has finished every place** (`search_and_score_keyword` scrapes all places, then
   scores). So the first job appears after about 2.5 min, even though the first scrape ended after about 1 min.

### Fewer jobs
4. **Skipped, not missing.** A job that was shown, or scored too low, is skipped on later searches. Repeating a search
   the same day finds mostly the same postings. Before `cb48bb3` the log and the page couldn't tell these apart.
   Deployed `counts` will give the exact split.
5. **Sources not contributing.** The three keyed APIs returned nothing in this search (see the table): settings, not code.
6. **Career-page checks starve.** The crawler holds the per-site slots (`ATS_HOST_CONCURRENCY` = 2), so live checks give
   up at once with `HostBusy`.
7. **Overlapping titles.** 57 % of the results were repeats (202 of 354), yet each title costs 2–4 min of scraping.

**Not proven:** that a specific code change reduced the number of jobs. No logs from the older version exist to compare, and
the fixes of 2026-11-01 only add jobs. Item 4 is the likely explanation; the counts on the page will confirm or reject it.

## 3. Proposed work

| Step | Change | Expected effect (estimates until measured) |
|---|---|---|
| **A. Show jobs sooner** | Score and show after each place, not after all places. Feeds and the index run once, with the first place. | First jobs at about 1–1.5 min instead of about 2.5 min |
| **B. Separate search from background work** | A second worker for `maintenance` + `email` with few slots; search and apply keep their own. Crawls can't delay a search. | No waiting behind crawls. Needs a second systemd unit, reversing the 2026-10-21 "one service" choice |
| **C. Fair turns between users** | A search queues at most N titles at a time (`SEARCH_TITLES_AT_ONCE`, N = 3); each finished title queues the next. | A second user starts within about one title's time rather than after the whole queue. One user alone may finish a little later |
| **D. Career-page checks get a slot** | Keep one per-site slot for live checks, and retry a busy site once rather than failing at once. | Fewer `HostBusy` failures, more career-page jobs |
| **E. Fewer repeated titles** | Warn on the Keywords page when titles mostly return the same jobs; stop retrying when a title's results are all repeats. | Less scraping per search |
| **F. Measure** | Show queue wait, scrape and scoring time per search in Admin → health; alert past a limit. | Proof the changes worked, and early warning |

Not proposed: more results per title or removing the pause. Both raise the risk of LinkedIn and Indeed blocking the server,
which would lose more jobs than it gains.

Tests for A, C and D use the existing fake pipeline. B is a deploy change: the worker settings are read from the unit file.

## 4. Facts needed from the server

```bash
systemctl cat job_finder_celery | grep -E "ExecStart|concurrency"
```

```bash
free -h && nproc
```

These decide how many slots each worker gets. Without them, the numbers in B are guesses.

## 5. Decisions for the owner (recommended defaults)

1. Do steps A, C and D first (code only, safe to deploy together). _Recommended: yes._
2. Add the second worker (step B), reversing "one service". _Recommended: yes, once §4 shows there is memory for it._
3. A user's search runs at most 3 titles at a time (step C). _Recommended: 3._
4. Step E and F after A–D are measured. _Recommended: yes._
5. Keep hiding already seen jobs by default ("each job shows once"). _Recommended: yes. The empty-search message now
   explains it and offers them back._
