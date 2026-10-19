# P4 — Apply Center: Manual, Fast, Auto and Email apply (draft for approval)

Status: **approved 2026-10-19 (all ten recommended defaults). P4a built on `dev` — see CLAUDE.md 6.9. P4b not started.**
P4a deviations: migration `0008` holds only what P4a needs (`jobs.apply_url`, `answer_bank`, `apply_packs`); `apply_runs`, `apply_proofs` and `apply_settings` come with P4b. The field mapper has no route of its own yet (the P4b engine calls it). Answer-bank matching needs 0.75 word overlap, not exact wording.
Inputs:
- CLAUDE.md §7.7 (Apply Center), §7.2 (answer bank), §12 (automation ethics).
- `../research/job-tools-research.md` §3 and §5 items 5–10.
Builds on P3–P3.2 + F3 on `dev` (`2b178c3`). The frontend counterpart is F4 (separate plan, after this one is approved).

---

## 1. Where we start

| Exists today | Gap |
|---|---|
| `POST /applications/manual` (F3): marks a job `submitted` / `manual` and moves it to Applied. | No "prep pack" (tailored resume, cover letter) before the user applies. |
| `POST /apply` → `apply_task`: Playwright on the worker opens LinkedIn jobs and fills first name, last name, email and phone in Easy Apply. Never submits. | Only 4 fields. The browser is **headed on the worker**, so it only works when the worker runs on the user's own PC. On the Oracle VM nobody can see it. |
| `Resume.parent_id` ("tailored variants point at their master"). | Nothing creates tailored variants yet. |
| `auto_apply` entitlement with `max_level` (default 1); kill switch `auto_apply_enabled`. | No apply runs, no caps, no review queue, no answer bank, no proof. |
| `applicant_info`, work authorization, relocation, EEO (encrypted) in the profile (P3.2). | No field mapper uses them. |
| JobSpy returns `job_url_direct`, the company's own apply link (often Greenhouse or Lever). | `job_sources._pick` keeps `job_url` and **drops the direct link**, so we can't tell which LinkedIn/Indeed jobs are really ATS jobs we could auto-apply to. |

---

## 2. Goal and acceptance criteria

Every job card has **four apply buttons**. A button is disabled, with the reason shown, when it can't work for that job.

| Button | What happens | Who submits | Tracker statuses |
|---|---|---|---|
| **Manual apply** | Prep pack (tailored resume + cover letter, optional) → open the job page → "Did you apply?" Yes / Not yet / Not interested. | User, by hand | `opened` → `submitted` (manual) |
| **Fast apply** | Fill the form, stop before submit. LinkedIn Easy Apply now, more later. | User clicks Submit | `prefilled` → `submitted` (manual) |
| **Auto apply** | Level 2 "review & submit" on a supported ATS (Greenhouse, Lever). The system fills the form; the user approves the answers; the system submits. | System, after approval | `queued` → `awaiting_review` → `submitted` (apply_engine) + proof |
| **Email apply** | AI writes a short application email + attaches the tailored resume, sent to an address found in the job post. | User sends it | `emailed` |

**Acceptance (CLAUDE.md §15 P4):**
1. A level-2 run completes on a **sandbox** Greenhouse and Lever page (recorded HTML fixtures + one local test page), ending with a proof screenshot.
2. **No double applications:** the same job, or the same company + title within 30 days, is refused with a reason.
3. **Hard stops hand control to the user:** captcha, login wall, OTP, unknown required field, file-type refusal or adapter error → run goes to `awaiting_input` or `needs_user`, never guesses.
4. Every new screening question goes into the **answer bank** as a draft. It is used for auto-submit only after the user approves it.
5. Caps and the kill switch stop runs at once; a disabled user's runs are cancelled (P1 rule).
6. Offline tests for all of the above; the existing 205 tests stay green.

---

## 3. Design

### 3.1 Apply runs (one engine for all four buttons)
- **New table `apply_runs`:** user_id, job_id, application_id, `mode` (manual | fast | auto | email), `level`, `adapter`, `state`, `plan` (JSON: fields + answers + sources), `needs_input` (JSON), `resume_id`, `cover_letter`, `proof_id`, `error`, started_at, finished_at.
  - Unique on `(user_id, job_id)` while a run is active, so one job never has two runs at once.
- **States:** `queued → preparing → (awaiting_input ⇄ preparing) → awaiting_review → submitting → submitted | failed | cancelled | needs_user`.
  - `needs_user` = a hard stop (captcha, login, OTP). The user finishes on the real page and marks it applied.
- **Engine steps** (CLAUDE.md §7.7): Preflight → Prepare → Fill → Review → Submit → Verify → Record.
  - **Preflight:** entitlement + level, kill switch, caps (3.6), dedupe, adapter available, job still open (HEAD/GET the apply URL).
  - Every step writes `record_status()` with `status_source="apply_engine"`, so the existing tracker shows the timeline with no changes.

### 3.2 Knowing which adapter fits a job
- Keep the direct apply link: store `job_url_direct` as `jobs.apply_url` (new column). Adapter detection looks at `apply_url` first, then `url`.
- `adapters.detect(url)`: `boards.greenhouse.io` / `job-boards.greenhouse.io` / `?gh_jid=` → greenhouse; `jobs.lever.co` → lever; `linkedin.com/jobs` → linkedin; anything else → none (Manual / Email only).
- The feed and `GET /jobs` get `apply_options`: which of the four buttons work, and the reason for each one that doesn't. This replaces `auto_apply_capable` (kept for one release).

### 3.3 Adapters (`app/apply/adapters/`)
Interface: `can_handle(job)`, `extract_form(...)`, `fill(page, plan)`, `submit(page)`, `detect_outcome(page)`.

| Adapter | Read the form | Fill + submit | Runs where | Levels |
|---|---|---|---|---|
| **Greenhouse** | Public Job Board API gives the question schema (`/v1/boards/{slug}/jobs/{id}?questions=true`). | Playwright, headless, on the `apply` queue. | Cloud worker | 1, 2 |
| **Lever** | Public postings API + the hosted form. | Playwright on the hosted form. The official apply POST needs the company's key for many postings, so we use the browser for all. | Cloud worker | 1, 2 |
| **LinkedIn Easy Apply** | DOM labels (existing code, extended to all common fields + resume upload). | Fill only — **never submits** (ToS; AIHawk dropped LinkedIn for this). | **User's own PC only** (headed browser + their logged-in profile). In production it shows "Fast apply needs the local agent (P8)". | 1 |
| Generic / unknown | — | — | — | Manual / Email only |

Workday, Ashby, SmartRecruiters and Recruitee stay in P8 (Workday needs an account per company).

### 3.4 Answering form fields (most trusted first)
1. **Deterministic profile mapping:** name, email, phone, links, location, notice period, current/expected CTC, work authorization, relocation, sponsorship. EEO fields are filled from the encrypted EEO answers and **never sent to an AI model**.
2. **Answer bank** (new table `answer_bank`): user_id, `question_hash` (normalised text), question, answer, `field_type`, `approved`, `uses`, last_used_at. Matches by hash, then by close wording (same normalisation used for job keys).
3. **AI answer**, grounded in the resume + job description (router task `form_answer`). Saved to the answer bank as an **unapproved draft**. A run that relies on an unapproved answer always stops at `awaiting_review`.
4. **Ask the user:** a required field still empty → `awaiting_input`. The user's answer is saved to the answer bank as approved.

The AI never invents facts: the prompt gets only the resume and profile, and must answer `UNKNOWN` instead of guessing; `UNKNOWN` → step 4.

### 3.5 Prep pack: tailored resume + cover letter
- **Tailored resume** (router task `resume_tailor`): reorders and rewrites the summary and bullet emphasis for the job, using **only facts already in the parsed master resume**.
  - A check step compares employers, titles, dates and skills against the master. Anything new is dropped and logged.
  - Saved as a child `Resume` (`parent_id` = master, label "Tailored — {company}") and rendered to **DOCX** with `python-docx` (already a dependency). PDF is a later option.
- **Cover letter** (router task `cover_letter`): 150–250 words, plain text, same grounding rule. Editable before use.
- Both are optional per run and cached per (job, master resume version), so a second click costs no AI call.
- `POST /apply/prep` returns the pack for Manual and Email apply; Fast and Auto apply use it automatically.

### 3.6 Caps, dedupe and safety
- **Caps** (admin-set defaults, per user, per day): auto submits 20; per platform (Greenhouse 15, Lever 15); 1 application per company + title per 30 days; LinkedIn Fast apply 25/day.
- **Pacing:** 30–90 s random gap between auto submits on the same platform; runs only inside the user's working-hours window (default 08:00–22:00 local).
- **Dedupe:** `(user_id, job_key)` in `applications` and `apply_runs`, plus company + normalised title within 30 days.
- **Risk notice:** the user must accept it once (stored with a timestamp) before level 2 is unlocked. The admin caps the level per user (existing `max_level`).
- **Kill switches:** global `auto_apply_enabled` (exists) + per user; both cancel queued runs.
- Never bypass a captcha or bot check; never log in for the user; browser profiles are per user on the worker (existing `_safe_profile_dir`).

### 3.7 Proof
- After submit: screenshot of the confirmation page + the matched confirmation text.
- Stored **encrypted in the DB** like resume files (new table `apply_proofs`); kept `APPLY_PROOF_RETENTION_DAYS` (90), then deleted by a maintenance-queue cleanup task on the existing beat.
- `GET /apply/runs/{id}/proof` (owner only).

### 3.8 Email apply
- **Find an address:** only from the job post itself (an `email` in the description, or an "apply by email" line). No guessing and no paid finder in P4; Find HR (Hunter/Apollo) stays in P5.
- **Compose:** subject + body from the cover letter, tailored resume attached.
- **Send:** in P4 the app gives a ready-to-send draft (copy button + `mailto:` link + resume download). Sending from the user's Gmail as a draft needs the `gmail.compose` consent from P5, so it moves there.
- Status `emailed` once the user confirms they sent it.

### 3.9 API
- `POST /apply/prep` `{job_id, tailor_resume, cover_letter}` → prep pack (task id when AI is needed).
- `POST /apply/runs` `{job_id, mode, level?}` → 202 + run id. Replaces `POST /apply` (kept, deprecated, for one release).
- `GET /apply/runs?state=` (the review queue), `GET /apply/runs/{id}`.
- `POST /apply/runs/{id}/answer` `{field_id: value}` → back to `preparing`.
- `POST /apply/runs/{id}/approve` (level 2 → `submitting`), `POST /apply/runs/{id}/cancel`, `POST /apply/runs/{id}/done` (the user finished a `needs_user` or Fast run by hand).
- `GET/POST/PUT/DELETE /answer-bank`, `POST /answer-bank/{id}/approve`.
- `GET /apply/settings`, `PUT /apply/settings` (level, working hours, accept risk notice).
- Admin: caps in `PATCH /admin/flags` / entitlements; `GET /admin/apply/runs` for support.

---

## 4. Steps
1. Migration `0008`: `jobs.apply_url`; `apply_runs`, `apply_proofs`, `answer_bank`, `apply_settings`; router tasks `form_answer`, `resume_tailor`, `cover_letter` as model-default rows.
2. Keep `job_url_direct` in `job_sources` / `job_store` → `apply_url`; `apply_options` in the feed and `GET /jobs`.
3. Prep pack: tailoring with the fact check, DOCX render, cover letter, cache.
4. Answer bank service + field mapper (profile → answer bank → AI → ask).
5. Engine: run state machine, preflight (caps, dedupe, kill switch), Celery task on the `apply` queue, `record_status` on every step.
6. Adapters: Greenhouse, Lever (headless), LinkedIn Easy Apply extended (local only, never submits).
7. Proof capture + encrypted storage + retention cleanup.
8. Email apply draft.
9. Routes, schemas, OpenAPI; deprecate `POST /apply` and `auto_apply_capable`.
10. Tests:
    - field mapping, answer-bank matching, `UNKNOWN` → `awaiting_input`;
    - tailoring fact check drops invented items;
    - every state transition, approve / cancel / answer;
    - caps, dedupe, kill switch, disabled user, working hours;
    - Greenhouse and Lever adapters on recorded HTML fixtures (incl. a captcha page → `needs_user`);
    - proof stored encrypted and deleted after retention;
    - EEO values never reach the router.
11. CLAUDE.md §6.9 status, §7.7 / §8 / §9 / §10 updates.

**New env (optional):** `APPLY_DAILY_CAP` (20), `APPLY_PLATFORM_DAILY_CAP` (15), `APPLY_COMPANY_COOLDOWN_DAYS` (30), `APPLY_PROOF_RETENTION_DAYS` (90), `APPLY_HEADLESS` (true in production), `APPLY_RUN_TIMEOUT_SECONDS` (300).

**Size:** the biggest phase so far. Suggested split so each part ships and is testable on its own:
- **P4a:** steps 1–4 + Manual apply with prep pack + answer bank + Email apply draft (no browser changes).
- **P4b:** steps 5–7 — engine, Greenhouse/Lever level 2, review queue, proof, caps; LinkedIn Fast apply extension.

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| ATS form layouts change and adapters break | Schema from the public API where it exists; recorded fixtures in CI; adapter errors → `needs_user`, never a half-submit; source breaker pauses an adapter after repeated failures |
| Captchas on Greenhouse boards (some use reCAPTCHA) | Detected → `needs_user`; the user finishes on the real page. Never bypassed |
| AI writes something untrue in a form or resume | Grounded prompts, `UNKNOWN` path, fact check on tailoring, unapproved answers block auto-submit |
| LinkedIn account restrictions | LinkedIn stays fill-only and local-only; Fast apply cap; the real fix is the P8 extension |
| Applying twice / spamming one company | Dedupe + company cooldown + daily caps + pacing |
| AI cost (tailoring + cover letter + answers per job) | Prep pack cached per job + resume version; answer bank reuse; free models through the router first |
| Proof screenshots hold personal data | Encrypted at rest, owner-only, 90-day retention |

---

## 6. Owner decisions needed before building

1. **Split P4 into P4a (prep pack, answer bank, Manual + Email apply) and P4b (auto-apply engine, Greenhouse/Lever, review queue, proof)?** *Recommended: yes.*
2. **Auto apply = level 2 only** (system submits after the user approves each run). Level 3 full auto stays in P8. *Recommended: yes.*
3. **First adapters: Greenhouse + Lever, both through the browser** (not Lever's apply API, which needs the company's key for many postings). *Recommended: yes.*
4. **LinkedIn Easy Apply: fill-only, never submits, and only when the worker runs on your own PC.** In production it is shown as "needs the local agent (P8)". *Recommended: yes.*
5. **Answer bank: AI drafts are never used for auto-submit until you approve them once.** *Recommended: yes.*
6. **Tailored resume as DOCX** (no new dependency); PDF later. *Recommended: yes.*
7. **Caps:** 20 auto submits/day, 15 per platform, 1 per company + title per 30 days, LinkedIn Fast apply 25/day, 08:00–22:00 window, 30–90 s between submits. *Recommended: yes.*
8. **Proof screenshots encrypted in the DB, deleted after 90 days.** *Recommended: yes.*
9. **Email apply in P4 = ready-to-send draft (copy / mailto + resume download);** sending as a Gmail draft waits for P5 OAuth. Addresses only from the job post, no paid HR finder. *Recommended: yes.*
10. **ATS resume checker (score /100 + fixes, research §5 item 8): defer to P4.1** after P4b. *Recommended: defer.*
