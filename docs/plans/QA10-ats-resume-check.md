# QA10 — Resume check: can an ATS read it, and does it fit the job? (2026-11-11)

Source: the owner asked for "ATS readability" checking (from an outside proposal), then said
"do more research then plan write". Nothing in the backend checks this today (searched 2026-11-11).
This is the item already listed but never built: CLAUDE.md Appendix B "ATS resume score + keyword-gap
report", `../research/job-tools-research.md` §5 item 8 ("score /100, issue list, one-click fixes"), and
P4 decision 10 ("defer to P4.1"). Section C below is the "one-click fix".

Branch `dev` in both repos. Nothing committed, pushed or deployed without the owner asking.
**Status:** approved 2026-11-11 with all eight recommended defaults; built (CLAUDE.md §6.25). Deviations: the keyword-fit cache lives in Redis, not `user_jobs.breakdown` (that dict goes to the feed as `match`); `skill_terms.json` has ~400 terms; no pypdf-vs-pdfminer order comparison (column detection covers it); a Download button was added to resume versions.

---

## 0. What the research says (short)

- **No ATS gives your resume a "score".** Workday, Greenhouse, Lever etc. *parse* the file into
  fields and recruiters *search* those fields. Even Jobscan says its match rate is "a visualization
  tool", not what the employer sees. So we say **"Resume check"** and **"Keyword fit"**, never
  "your ATS score".
- **What actually breaks parsing** (vendor blogs agree; per-ATS claims are not verified, Workday and
  Taleo are said to be the strictest):
  1. no text layer (scanned / exported as image, Canva/Figma PDFs);
  2. two columns, side-by-side text, tables used for layout → text comes out in the wrong order;
  3. text boxes and shapes → often skipped;
  4. contact details in the page header/footer → often skipped;
  5. icon fonts / fancy bullets / broken ligatures → "of?ce", boxes, `(cid:12)`;
  6. non-standard section names ("My journey" instead of "Experience");
  7. dates the parser can't read, so experience years come out wrong.
- **Keyword fit:** the common target is about **75 %** of the job's hard skills (Jobscan says 75 %,
  65 % often works). Hard skills and job title weigh most; soft skills least.
- **Libraries:** PyMuPDF has the best column detection but is **AGPL** — running it in our hosted API
  would oblige us to publish the backend source (or buy a licence). **pdfminer.six (MIT)** gives text
  blocks with positions, enough to detect columns and header/footer text. A common trick in open
  checkers: extract the text two ways (pypdf, which we already use, and pdfminer) and flag pages
  where the reading order disagrees.

Sources: blog.scale.jobs/ats-parsing-errors-causes-and-fixes, atsverification.com/blog/10-most-common-ats-parsing-failures,
wildandfreetools.com/blog/ats-resume-tables-columns-why-they-fail-fix, jobscan.co (match-rate pages),
dev.to/nilamadhab47 (ATS-view tool write-up), github treejamie/maybe-ats-parse.

## 1. What already exists (reuse, don't rebuild)

| Already built | Where |
|---|---|
| PDF/DOCX text extraction, scanned-PDF detection (`parse_status = no_text`) | `services/resume_store.py` |
| AI resume parse → `ParsedResume` (experience, dates, skills…) | `resume_store.parse_resume`, task `resume_parse` |
| Per-job match breakdown: skills matched / missing, title fit, `missing_required` | `search_profiles.match_breakdown`, `scorer.py`, JobCard "Asks for: …" |
| Tailored resume per job (grounded — never adds facts) + plain single-column DOCX | `apply_prep.tailor`, `apply_prep.render_docx` |

So QA10 adds two things: **(A) a file check per resume** and **(B) a keyword-fit check per job**,
plus (C) a one-click plain copy for resumes that fail.

---

## 2. A — Resume check (per resume, no AI, free)

New `app/services/resume_check.py`, `check(content_type, data, text, parsed) -> dict`.
Runs on the worker right after text extraction, before AI parsing (it needs no AI), and again when
the user saves the reviewed parse (checks 7–8 use `parsed`). Tailored copies are checked too.

| # | Check | PDF | DOCX | Result |
|---|---|---|---|---|
| 1 | **Real text** | chars per page from pypdf; page with < 200 chars but images → "this page is a picture" | — | fail if no text, warn if some pages |
| 2 | **Odd characters** | count `(cid:n)`, U+FFFD, private-use glyphs (icon fonts), lone `?` inside words | same on text | warn > 0.5 % of chars, fail > 3 % |
| 3 | **Columns / reading order** | pdfminer text boxes: two or more boxes side by side over > 30 % of a page's height = columns; also pypdf vs pdfminer order disagreement | tables whose cells hold > 40 words (layout tables), `w:cols` with num > 1 in section props | warn |
| 4 | **Text boxes / shapes** | — | count `w:txbxContent` in `document.xml`; words inside them | warn, fail if contact or a section is inside one |
| 5 | **Contact details reachable** | email/phone found in the body (not only in the top/bottom 6 % band repeated on every page) | email/phone only in `header*.xml` / `footer*.xml` | fail when only in header/footer |
| 6 | **Standard section headings** | look for Experience / Education / Skills (+ synonyms list: "Work history", "Employment", "Technical skills"…) as short standalone lines | same | warn per missing section |
| 7 | **Readable dates** | every `parsed.experience` item has a start date the date parser understands (reuse the P3.1 posted-date parser rules) and end or "Present" | same | warn, list the jobs |
| 8 | **Length** | pages and words; > 2 pages with < 5 years, > 3 pages ever, < 200 words | words only | warn |
| 9 | **File** | encrypted (already refused), > 2 MB, file name like `resume (3) final.pdf` | same | info only |

**Score:** 100 minus weights per failed/warned check (fail 25, warn 8, capped), shown as
**Good (≥ 85) / Needs fixes (60–84) / Hard to read (< 60)** with every finding as one plain line plus
a fix ("Move your phone and email out of the page header into the first line of the page.").

**"What a resume reader sees":** the pdfminer text in its reading order (DOCX: body text without
header/footer/text boxes), so the user can see the jumble for themselves. Stored only as part of the
check result, never logged.

Stored on `resumes`: `ats_check` (JSON: version, score, band, findings[], seen_text_excerpt ≤ 6,000
chars) and `ats_checked_at`. Migration **0027** (additive, nullable). Old resumes get checked
lazily the first time their detail is opened (file is decrypted on the worker, not in the request).

## 3. B — Keyword fit (per job + resume, no AI)

- New data file `app/data/skill_terms.json`: ~600 hard-skill terms with aliases
  (`"postgresql": ["postgres", "psql"]`, `"aws bedrock"`, `"langchain"`, `"rag": ["retrieval augmented generation"]`…),
  plus a short soft-skill list. Built once by hand + from existing search-profile skills; reviewed in the PR.
- `resume_check.keyword_fit(job_text, job_title, resume_text) -> dict`:
  - hard skills the job mentions (dictionary hits, plus the scorer's `missing_required` when present),
    split into **found in resume** / **missing**;
  - job title or a close variant (reuse `title_fit`) present in the resume;
  - education level only when the job names a degree;
  - `fit` % = hard-skill coverage 70 % + title 20 % + education 10 %.
- `GET /api/v1/jobs/{job_id}/keyword-fit?resume_id=` (default: master). Cached in the existing
  `user_jobs.breakdown` JSON under `keyword_fit` keyed by resume sha, so re-opening is free.
- Prep pack: after tailoring, show **before → after** ("62 % → 78 %"). The tailor stays grounded:
  missing skills are listed as "add only if you really have it", never written in by the AI.

## 4. C — Plain copy for a resume that fails

When the check band is "Hard to read" and the resume has a reviewed parse, offer
**"Make a plain copy"**: `render_docx(parsed)` (already single-column, standard headings), stored as a
new resume version with label "Plain copy of …" (does not count toward the upload limit, like tailored
copies). The user downloads and checks it; we never replace their master on our own.
A test makes sure our own `render_docx` output scores **Good**.

---

## 5. API

- `ResumeOut` / `ResumeDetail` gain `ats_check` (nullable) and `ats_checked_at`.
- `POST /api/v1/resumes/{id}/check` → 202, re-runs the check (rate-limited like re-parse).
- `POST /api/v1/resumes/{id}/plain-copy` → 201 new `ResumeOut` (409 `resume_not_reviewed` without a reviewed parse).
- `GET /api/v1/jobs/{job_id}/keyword-fit?resume_id=` → `{fit, hard_found[], hard_missing[], title_found, education}`.
- Regenerate frontend types from OpenAPI.

## 6. Frontend

- **Profile › Resume** (`features/profile/ResumeSection.tsx`): each resume row gets a check badge
  (Good / Needs fixes / Hard to read). Expanding shows the findings with fixes, a
  "See what a resume reader sees" toggle (monospace text box), "Check again" and, when hard to read,
  "Make a plain copy".
- **Prep dialog** (`features/apply/PrepDialog.tsx`): a "Keyword fit" block — fit %, found chips,
  missing chips ("add only if true"), and before → after once the tailored resume is ready.
- **Job card**: nothing new (the match breakdown already shows missing skills); avoids clutter.
- MSW mocks for the three endpoints in `src/mocks/`.

## 7. Tests

- Backend fixtures in `tests/fixtures/resumes/` (generated in the test, not real people's resumes):
  clean one-column PDF and DOCX → Good; two-column PDF → columns warning; DOCX with contact in the
  header → fail; DOCX with a text box; DOCX layout table; PDF with icon-font glyphs / `(cid:…)`;
  image-only PDF → fail; `render_docx` output → Good.
- keyword_fit: aliases ("Postgres" = PostgreSQL), word boundaries ("Java" ≠ "JavaScript", "Go" only
  as a skill, "C++" / "C#"), missing list, title found, cache reuse.
- API: lazy check, re-check rate limit, plain copy 409/201 and upload limit untouched, other user's
  resume 404.
- Frontend: badge per band, findings list, toggle, plain-copy button only when hard to read, Prep
  dialog fit block. `npm run check` green (`npx vitest run --maxWorkers=2`).
- Then a live look with the owner's real resume on the local app (SQLite, never Supabase).

## Order

Backend A (service + migration 0027 + tests) → B → C → API → frontend → full `pytest` +
`npm run check` → CLAUDE.md §6.25 + tick Appendix B → commit on `dev` (author Avanish Vishwakarma, no Claude trailers). No push, and no
`alembic` against the `.env` database, until the owner says so.

---

## 8. Decisions for the owner

1. **Name on screen:** "Resume check" + "Keyword fit" — *recommended* (no ATS really scores you) —
   or "ATS score"?
2. **PDF library:** pdfminer.six (MIT, free to host) — *recommended* — or PyMuPDF (better columns,
   but AGPL: publish the backend source or buy a licence)?
3. **AI in the check:** none — *recommended* (free, instant, works on Free tools only) — or add an
   optional AI review of wording (uses the AI quota)?
4. **Keyword list:** curated `skill_terms.json` (~600 terms, predictable) — *recommended* — or ask
   the AI to pull keywords from each job (catches rare terms, costs a call per job)?
5. **Target line:** show 75 % as the "good fit" line — *recommended* — or no target line?
6. **Plain copy (section C):** offer it only when the check says "Hard to read" — *recommended* —
   or for every resume?
7. **Who gets it:** every user, no entitlement — *recommended* (no paid calls) — or behind a feature switch?
8. **Old resumes:** check lazily when opened — *recommended* — or a one-time backfill job on deploy?
