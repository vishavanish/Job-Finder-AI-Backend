# QA2 — Phase 2: search speed, reliability and data quality (2026-10-25)

Follows QA1 (`QA1-quick-wins.md`, built and pushed to `dev`). Source: the live QA pass on
2026-10-25 and a read-only look at production (`usage_ledger`, `ai_models`).
Branch `dev` in both repos; nothing pushed or deployed without the owner asking.

## 0. What the data says

- A search for 4 titles showed **1 job after 2.5 minutes** ("0 of 4 titles done").
- Each title is one Celery task (`search_and_score_keyword`): it **scrapes every location in
  turn** (the profile has `India` and `Mumbai`, so LinkedIn/Indeed run twice, with a 15–18 s
  pause between JobSpy searches), and only **then scores the jobs one by one** — one AI call
  per job.
- The free AI tier is the real limit: only **2 text models work** (Gemini Flash-Lite, Groq
  GPT-OSS 120B), both rate-limited under load (45 of the last 100 failures were 429s, all
  from scoring). 4 titles × ~25 jobs = ~100 calls; at free-tier rates that alone is minutes.
- The worker runs with `--concurrency=2` (`start.sh`), so 2 of the 4 titles wait.

## 1. Faster search (the main item)

1. **Score in batches.** Score up to `SCORE_BATCH_SIZE` (5) jobs per AI call with the batch
   prompt that already exists (`scorer.score_jobs`), instead of one call per job. About
   **5× fewer calls**, so far fewer rate limits. Jobs still appear in the feed batch by batch
   (every few seconds), not all at the end. A batch whose reply can't be parsed falls back to
   one-by-one for those jobs only.
2. **Show jobs as soon as they are found.** While a batch is being scored, push the job to
   the feed as `scoring: true` (card shows "Scoring…" instead of a fit score), then update it
   in place (`replaces: true`, the mechanism P3 already has). The user sees results within
   seconds of the first source answering.
3. **Don't search the same place twice.** When a location is inside another one in the
   same search (`Mumbai` inside `India`), search only the wider one and keep the narrower one
   for filtering. Halves JobSpy time for this profile.
4. **Time every stage.** Per run: seconds per source, per scoring batch, AI calls and
   rate-limits — stored in the run meta, shown in the Activity tray detail ("LinkedIn 38 s ·
   scoring 12 s") and in admin health. So the next slow search can be explained.
5. **Ops check:** the server's `job_finder_celery` unit — confirm its `--concurrency`; raise
   to 4 if the VM has the memory (Playwright apply runs share it).

**Done when:** the same 4-title search shows the first jobs in < 30 s and finishes in < 2 min
on the free models, with no "unscored" jobs from rate limits.

## 2. Apply prep pack

- Tailored resume and cover letter run **one after the other** today; run them **in
  parallel** (two calls, two providers when both are up).
- The dialog shows **live steps** ("Tailoring resume ✓ · Writing cover letter…") from the pack
  status instead of one "10–40 s" line, and the time estimate comes from the last packs'
  real durations.

## 3. Profile filled from the resume

- After a resume is parsed (or reviewed), **fill empty Applicant info fields** from it:
  phone, city, LinkedIn/GitHub, years of experience. Never overwrite what the user typed.
  A "Use from resume" link next to each empty field shows the value first.
- Resume parse: ask for and keep **total years of experience** (empty today although the
  resume says 3 years).
- Clean pasted URLs (drop `?isSelfProfile=true` and other tracking parameters).

## 4. ATS targets data quality

- Default the location filter to the user's preferred locations (+ Remote); one click to
  clear.
- Collapse the same job posted for many countries into one row with "+4 locations".
- Company names: prefer the name from the careers page/feed over the slug (fixes "Openai",
  "Wk", "Bjakcareer"); admin can rename (exists).
- Hide postings older than `ATS_MAX_POSTING_AGE_DAYS` (90) unless "Any time" is picked.
- One date style ("3 days ago" up to 30 days, then "12 Jun 2026").
- One platform count everywhere (from `ats_platforms`), and drop Homerun from the filter.

## 5. Small UI fixes from the QA list

- Sign-in error under the email field, linked with `aria-describedby`, field focused.
- Favicon and per-page titles on the sign-in page; `<meta name="description">`.
- Skill-gap chips de-duplicated ("MongoDB" vs "PostgreSQL / MongoDB").
- Mobile pass of every signed-in screen (375 px), fix what overflows.

## Order

§1 (biggest user impact) → §2 → §3 → §5 → §4. Each with tests; backend `pytest` and frontend
`npm run check` green; commit per section on `dev`.

## Decisions for the owner

1. **Batch size 5** for scoring — *recommended* (5× fewer AI calls, results arrive in small
   groups) — or 3 (results feel more "live", ~3× fewer calls)?
2. **Show jobs before they're scored** ("Scoring…" on the card) — *recommended* — or only
   show scored jobs as today?
3. **Skip narrower locations** (Mumbai when India is also searched) — *recommended* — or keep
   searching each location as typed?
4. **Worker concurrency 4** on the server — *recommended if the VM has ≥ 4 GB free* — I can
   only check this with SSH access or you run `free -h` and `systemctl cat job_finder_celery`.
5. **Fill Applicant info from the resume automatically** (empty fields only) — *recommended* —
   or only offer "Use from resume" links?
