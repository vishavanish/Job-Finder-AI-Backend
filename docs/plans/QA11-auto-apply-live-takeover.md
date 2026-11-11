# QA11 — A stronger Auto apply: sign in yourself mid-run, then it carries on (2026-11-11)

Source: the owner asked (2026-11-11) to make Auto apply "more powerful": when a site asks for a login, pause
for the human (HITL), let them give what's needed, then the workflow goes ahead — plus any better ideas,
including for interviews.

Branch `dev` in both repos. Nothing committed, pushed or deployed without the owner asking.
**Status:** all eight recommended decisions approved 2026-11-11. **Part A built** (CLAUDE.md §6.26). Deviations: captchas inside a form still go to needs_user (a check solved in a separate browser doesn't carry over); Web Push moves with part D (notifications). Next: D, then C, B, E, F.

---

## 0. Where we are today

- A sign-in page, an account form, a captcha or an OTP is a **hard stop** (`HardStop` → run `needs_user`).
  `forms.login_wall` = a visible password box; the AI form reader also labels pages `login` / `account_create`.
- Account-only career suites are never even tried: `sites.ACCOUNT_SITES` = Workday, iCIMS, Taleo, BrassRing,
  SuccessFactors, Oracle Cloud, UKG, ADP, Darwinbox, PeopleStrong, Phenom, Avature. In India most big
  employers (banks, IT services, MNCs) hire through these — so a large share of good jobs is "Manual only".
- The engine **does not keep the browser between steps** (P4b): each step opens a fresh headless Chromium and
  reads the form again. A login made in one step would be lost in the next.
- One HITL path exists: **"Open and fill in my browser"** (P8b E3b) — the run moves to the user's own
  Chrome with the extension, which fills the form there. It needs the extension and a desktop, and the
  server-side run doesn't continue after it.
- Blueprint rules that stay: never bypass captcha/bot checks, never invent answers, auto-submit off by
  default, no secret in logs / Celery args / API responses (§0, §7.7, §12).

## 1. What the research says (short)

- **"Live view + human takeover" is the standard pattern** for agent browsers: Browserbase Live View,
  Steel debug URLs, Cloudflare Browser Run, Browser Use, Browserless `liveURL`. The agent pauses on a
  checkpoint (login, 2FA, captcha, unexpected modal), the person works **in the same live browser**, then
  marks it done or the script notices the page changed, and the agent continues where they left off.
- **Self-hosting it:** two ways.
  - *CDP screencast* — `Page.startScreencast` frames to a `<canvas>`, mouse/keys sent back with
    `Input.dispatch*`. Rides the Playwright connection we already have; no new system packages; fastest MVP.
    Practitioners: fine to watch, input is a bit less dependable (IME, file pickers, some widgets).
  - *Xvfb + noVNC* — a real virtual display streamed by VNC. The "boring production choice", most reliable
    input, but more moving parts and CPU per session.
  - Hosted services (Browserbase, Steel) would work at once but send the user's typed password through a
    third party and cost per minute.
- **Workday** (the biggest account suite): an account **per employer tenant**, email verification with a
  6-digit code on many tenants, duplicate profiles when name/email/phone differ slightly; then a fixed
  multi-page form (My Information → My Experience → Application Questions → Voluntary Disclosures →
  Self Identify → Review). Workday parses the uploaded resume and pre-fills My Experience.
- **Competitors** mostly don't solve this: Simplify autofills Workday but doesn't submit, LazyApply /
  JobCopilot are job-board heavy. Kombo's AI Apply creates an account per application with a forwarding
  email — fully automated account creation, which we won't do (it fakes the human step). **A clean,
  human-in-the-loop Workday path is a real edge.**

Sources: docs.steel.dev (human-in-the-loop), developers.cloudflare.com/browser-run (human in the loop),
docs.cloud.browser-use.com (human takeover), browserbase.com (Live View case study),
docs.browserless.io (hybrid automation), github.com/xtr-dev/mcp-playwright-novnc, playwriter screencast,
doc.workday.com (candidate home / verification email), froghire.ai + jobwizard.ai (account per employer),
docs.kombo.dev (AI apply account creation), scoutify.com + jobwizard.ai tool comparisons.

---

## 2. The idea in one picture

```
Auto apply ─► page asks to sign in / create account / verify / captcha
               │
               ▼  (instead of "needs you — go apply yourself")
        run = awaiting_login   ── notify: in app + email (+ push, §6)
               │
        user opens "Sign in to continue" ─► sees the real page live, types email,
               │                            password, the code — themselves
               ▼
        page no longer asks to sign in  ─► session cookies handed to the engine
               │
               ▼
        engine carries on as today: fill → review → (approve) → submit
        optional: keep the sign-in for this site so next time there's no stop
```

The user types their own password and code into the real site. **We never store a password** and never
type one. A captcha the user solves themselves is a human doing it, not a bypass — allowed.

---

## 3. A — Live takeover ("Sign in to continue")

**Backend**
- New active run state **`awaiting_login`** (also used for captcha / verify-email / "I am human" pages).
  `plan.takeover = {reason: login|account|verify|captcha, url, started_at}`.
- New small service **`job_finder_browser`** (one systemd unit, async Playwright, one process): owns the
  live browsers. It launches Chromium for the run (same viewport, user agent and locale as the engine — we
  don't disguise anything), goes to the stop page, and streams it. Talks to the API over Redis
  (start/stop/input) and a localhost WebSocket for frames. Keeps the slow, heavy browser out of the API and
  out of Celery worker slots.
- `WS /apply/runs/{id}/live` (API, like the voice sockets): single-use token (60 s), owner only, run must be
  `awaiting_login`. Server → client: JPEG frames (`Page.screencastFrame`, ≤ 10 fps, quality 60), URL,
  "page looks signed in". Client → server: mouse, wheel, keys, text, resize, `done`, `cancel`.
- **Detection of "done":** after each navigation, the page has no visible password box and no
  login/account/verify wording, *and* a form or the job page is visible (`forms.login_wall`, the form
  reader's `page_kind`) → show "Looks like you're in — Continue". Auto-continue after 3 s unless the user
  says wait. The user can always press **Continue** or **Give up**.
- **Hand-back:** the live browser's `context.storage_state()` (cookies + local storage) is sealed
  (`key_store.seal`) into Redis with a 30-min TTL under the run; the live browser closes; the run goes back
  to `queued` and the normal engine opens its fresh browser **with that storage state**. So the engine's
  "fresh browser per step" design stays. Each later step of the same run reuses it.
- **Limits:** `LIVE_SESSIONS_MAX` at once on the server (default 2 — the OCI VM's memory), idle 5 min →
  paused, 15 min total → back to `needs_user` as today (the "Open in my browser" button still works),
  `LIVE_SESSION_DAILY_CAP` per user. Frames are never stored; nothing typed is logged (the input path logs
  event counts only). Admin health shows live sessions in use.
- **Helpers in the panel** (never typed without a click): "Type my email" (sign-in email or applicant email),
  "Type my phone". For an account form: a note to use a password manager; we don't generate or keep one.

**Frontend**
- Run page `/apply/runs/:id` for `awaiting_login`: hero "Workday wants you to sign in — do it here and we'll
  carry on", the live canvas (fits the screen; works with mouse, keyboard and touch), Continue / Give up,
  the helper buttons, a countdown. "Open in my browser instead" kept as the alternative.
- Apply Center "Needs you" tab shows these first, with how long the session stays open.

## 4. B — Remember my sign-in on this site (optional, decision 3)

- After a successful takeover: checkbox "Keep me signed in on <tenant> for 30 days".
- Stored per user + site (Workday: per tenant host) in a new table `apply_site_sessions` (sealed storage
  state, created/last used/expires, site). Used by the engine when opening that site. Expired or
  rejected → deleted, and the next run asks for takeover again.
- Settings › Apply: list of kept sign-ins with Remove; disabling a user or "delete my data" removes them.
- Never kept for job boards (LinkedIn, Naukri, Indeed) — those stay with the extension (user's own browser).

## 5. C — Workday (and then Oracle Cloud / SuccessFactors) adapter

With A in place, the account suites stop being a dead end:
- Take Workday (`myworkdayjobs.com`, `myworkdaysite.com`) out of `ACCOUNT_SITES` for Auto apply; the run
  starts, meets the sign-in, the user signs in / creates the account live, then a **Workday adapter** fills
  the known pages (automation ids like `data-automation-id="legalNameSection_firstName"` are stable across
  tenants), uploads the resume, then **checks Workday's resume pre-fill** against the parsed resume
  (dates, titles) and fixes what it got wrong. Unknown questions → answer bank → AI → ask the user
  (unchanged rules). Stops before Submit for review, as level 2 does today.
- Same identity every time (name, email, phone exactly as in Profile) to avoid Workday duplicate profiles.
- Then Oracle Cloud and SuccessFactors with the generic reader + takeover (they're already on the job index
  from QA7). iCIMS / Taleo later.
- Live read-only check on 3 real Workday tenants before turning it on (like P4b).

## 6. D — Tell the user fast (HITL only works if they see it)

- A takeover waits at most 15 min, so the nudge matters. New `notifications` table (already in the
  blueprint §8, never built): type, payload, read_at. In-app bell + Activity tray entry.
- Email (existing SMTP) when a run needs you and you're not in the app; **Web Push** (VAPID, `pywebpush`,
  service worker in the frontend) so a phone or desktop pings — optional per user.
- Quiet hours follow the user's apply hours.

## 7. E — More power, smaller steps (each can ship alone)

1. **Apply queue / "Apply to my top N":** pick jobs from Jobs (Strong matches), one click queues Auto apply
   for each; one review screen to approve them; caps, pacing and duplicates as today.
2. **Questions up front:** before a run starts on a known site (Greenhouse / Lever / Ashby boards publish
   their questions), list the questions we can't answer yet so the user answers once, then the run never
   stops for them.
3. **Site memory:** remember per site/tenant which field got which answer key and which button was Next,
   so repeat sites need no `form_map` / screenshot AI calls (cheaper, faster, fewer stops).
4. **Resume pick per job:** use the QA10 keyword fit to choose between the master and a tailored copy
   automatically, and warn when the resume check says "Hard to read".

## 8. F — Interview idea: from "applied" straight into prep

- When Gmail marks an application **interview_invite** (P5 already detects it), create an interview pack:
  a Practice session preset with that job's description, the company, the role level and the questions
  most likely from the JD + our resume gaps (QA10 keyword fit `hard_missing`), and a story-bank check
  ("you have no story for leadership").
- Notify: "Orbit Labs invited you to interview — 20-min practice ready". Dashboard card "Upcoming
  interviews" with the date when the email has one.
- Uses only what exists (P7 Practice, story bank, P5 classifier); no new AI provider.

---

## 9. Decisions for the owner

1. **How the live view is built:** CDP screencast in our own browser service — *recommended* (no new
   system packages on the Oracle Linux VM, fastest to build; noVNC later if input proves flaky) — or
   Xvfb + noVNC now, or a hosted service (Browserbase/Steel: fastest, but passwords pass through a third
   party and it costs per minute — *not recommended*).
2. **Where live browsers run:** a separate `job_finder_browser` service — *recommended* — or inside the
   API process (simpler deploy, but a heavy Chromium in a web worker).
3. **Keep sign-ins (section B):** yes, opt-in per site, 30 days, sealed — *recommended* — or never keep
   (sign in every time).
4. **Verification codes:** the user reads and types the code — *recommended*; optional extra: when Gmail is
   connected, *show* the code from the site's email in the panel for the user to copy (they still type it).
   Never type it for them, never create accounts on our own.
5. **Order:** A → D (in-app + email) → C Workday → B → E → F — *recommended* — or F (interview) first
   because it is quick and uses only what exists.
6. **Limits:** 2 live sessions at once, 15 min each, 10 per user per day — *recommended* (raise after
   checking `free -h` on the server).
7. **Who gets it:** users with `auto_apply` level 2 and the risk notice accepted, plus a new kill switch
   `live_takeover_enabled` — *recommended*.
8. **Web Push (section D):** build it with A — *recommended* — or email only for now.

## 10. Order and checks (after approval)

A (browser service, state, socket, hand-back, tests with a local fixture site that has a sign-in page →
form, plus a fake captcha page the "human" clicks) → D → C (fixtures copied from 3 Workday tenants, then a
live read-only check) → B → E → F. Each part: pytest + `npm run check`, CLAUDE.md section, commit on `dev`.
The server needs the new systemd unit and memory headroom; I'll give one paste-ready block when we deploy.
