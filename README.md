# Job Finder AI API

A FastAPI backend that automates the end-to-end job search workflow: scrapes job
postings from multiple sources, scores them against a candidate profile using an
LLM, opens/pre-fills applications in a real browser, and tracks application status
— including auto-detecting rejections and interview invites from Gmail replies.

Every configurable value (keywords, locations, skills, resume, thresholds, ATS
targets) is part of the request body, not a hardcoded config file — the same API
can drive different searches per call without redeployment.

## Stack

- **FastAPI** — REST API layer, JWT auth, per-user scoping
- **Celery + Redis (Upstash)** — background task queue for search/score/apply/pipeline
- **SQLite** — application tracking + user accounts
- **Playwright** — browser automation for opening/pre-filling job applications
- **Gemini / Hugging Face (Qwen)** — LLM-based job-fit scoring, with automatic fallback
- **Gmail API** — polls one or more inboxes to auto-detect application status changes

## Auth

All endpoints except `/auth/register`, `/auth/login`, and `/health` require a
Bearer JWT, obtained via login and sent as `Authorization: Bearer <token>`.
Tokens are scoped per user — every resource (applications, tasks) is filtered to
the authenticated caller only.

## Core Endpoints

| Method | Path                          | Description |
|--------|-------------------------------|-------------|
| POST   | `/api/v1/auth/register`       | Create an account |
| POST   | `/api/v1/auth/login`          | Log in, returns JWT + user info |
| POST   | `/api/v1/search`               | Search jobs across LinkedIn/Indeed/Naukri (async, returns task_id) |
| POST   | `/api/v1/score`                 | Score a list of jobs against a candidate profile via LLM (async) |
| POST   | `/api/v1/apply`                 | Open/pre-fill top-matched jobs in a real browser (async) |
| POST   | `/api/v1/apply/{task_id}/close` | Close a still-open apply browser session |
| POST   | `/api/v1/pipeline/run`          | Run search → score → (optional) apply as one orchestrated flow (async) |
| GET    | `/api/v1/tasks/{task_id}`       | Poll status/result of any async task |
| POST   | `/api/v1/company-ats/fetch`     | Pull jobs directly from a company's career page (Greenhouse/Lever/SmartRecruiters/Workday/SuccessFactors/custom) |
| GET    | `/api/v1/applications`          | List your tracked applications (paginated, filterable by status/company/source/date) |
| GET    | `/api/v1/applications/{id}`     | Get a single application record |
| PATCH  | `/api/v1/applications/{id}`     | Manually override an application's status |
| GET    | `/health`                       | Service health check (DB writable + at least one LLM key configured) |

## Async Task Pattern

`/search`, `/score`, `/apply`, and `/pipeline/run` all dispatch to Celery and
return `{task_id, status, ...}` immediately (HTTP 202). Poll
`GET /tasks/{task_id}` until `status` is `completed` or `failed` — it never 404s,
so callers must enforce their own timeout/max-attempts when polling.

## Application Status Tracking

Every job opened via `/apply` is logged with `status_source=browser_apply`.
Statuses can also be set manually (`status_source=manual` via `PATCH`) or
detected automatically from Gmail replies (`status_source=email_parser`, in
progress). Valid statuses: `opened`, `submitted`, `interview`, `rejected`,
`offer`, `ghosted`.

## Gmail Integration (in progress)

Polls one or more Gmail accounts (configurable via `GMAIL_ACCOUNT_LABELS`) for
recent mail, pre-filters candidates that look like ATS/recruiter replies, and
(pending) classifies them via LLM into rejection/interview/other before updating
the matching `Application` row. Handles the case where you apply from one
address but replies land in a different inbox.