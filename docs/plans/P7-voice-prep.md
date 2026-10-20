# P7 — Voice Interview Prep: Practice mode (mock interviewer) + Live Copilot (web)

Status: **approved 2026-10-20 (all recommended defaults + OpenRouter fallback / OpenAI ready, decisions 3a/3b). P7a Practice committed on `dev` (`3294b39`); P7b Live Copilot built after it — see CLAUDE.md 6.13.**
Deviations in P7a:
- LLM `stream()` adapters are synchronous generators run in a worker thread (`services/voice_llm.py`), not `async def` — the same adapters serve Celery.
- Deepgram streaming STT and Deepgram Aura / ElevenLabs TTS move to P7b; P7a ships Groq Whisper + OpenAI-compatible STT/TTS + browser voice.
- The socket also takes typed answers (`{"type": "text"}`) for keyboard users.
- Story drafts use the `interview_chat` task; scores are 1–5.
- `voice_bench.py` and `GET /admin/voice/health` stay in P7b step 11 (as planned).
Deviations in P7b:
- Deepgram is the only streaming STT built; Deepgram Aura / ElevenLabs voices are not built (Practice uses browser or OpenAI voices).
- The user's own mic channel uses the per-utterance model even when Deepgram is available (cheaper; it is context only).
- `question.detected` carries the type as `qtype` (`type` is the message envelope's field); also `question.updated` for speculative restarts and `answer.cancelled` reasons `restarted` / `regenerating` / `no_model`.
- Mic-only diarization: the user marks their own speaker once with the `that_was_me` control (nothing is guessed).
- `answer.done` adds `unverified` (names/numbers not found in the resume, stories or job) for personal question types.
- The question-gate eval set has 125 lines (plan said ~150). The screens are F7 (separate plan: `job-finder-frontend/docs/plans/F7-interview-prep.md`).
Inputs:
- CLAUDE.md §7.10 (Practice + Live Copilot, WebSocket protocol, latency budget, transparency rules), §7.9 (registry/router), §8 (`voice_sessions`, `voice_turns`, `voice_feedback`, `story_bank`), §9 (Voice endpoints), §12 (privacy), §15 (P7 acceptance).
- Today's code: `ai_router.TASKS` already lists `interview_chat` / `interview_eval` (comment: "stt/tts/embeddings arrive with P7"); entitlements `voice_prep` (on) and `live_copilot` (**off**) already exist in `services/entitlements.py`. LLM adapters (`providers/llm/adapters.py`) are **synchronous `complete()` only** — no streaming, and the app has **no WebSocket route yet**.

Builds on P6 on `dev` (`541c1bb`; migration `0011`).

---

## 1. Goal and acceptance criteria

1. **Practice mode (mock interviewer)** over `WS /voice/sessions/{id}/stream`:
   - setup: a chosen job (its description) or a typed role, interview type (behavioral/STAR, technical, system design, role-specific from job + resume, rapid-fire, "walk me through your resume"), difficulty, length (5 / 10 / 20 questions or minutes);
   - one question at a time, spoken (TTS) **and** captioned; the user answers by voice; the interviewer follows up or moves on; barge-in stops the interviewer's speech;
   - after the session: per-answer and session feedback (content accuracy, relevance, STAR structure, clarity, filler words and pace from timestamps, strengths, improvements, a **model answer grounded in the user's real resume/story bank**) and progress per skill area.
   - **Target: first spoken word ≲ 1.5 s p50** after the user stops speaking (measured with the recommended provider set, §2.2).
2. **Live Copilot (web)** over `WS /voice/copilot/{id}/stream`, exactly the protocol in CLAUDE.md §7.10:
   - mic enabled once → continuous loop: interviewer-channel speech → question gate → card with streamed answer; follow-ups thread under their parent; multi-part → numbered points; small talk ignored; **Answer this**, **Dismiss**, **Shorter / Expand / Regenerate**;
   - speculative start on a partial transcript; **cancel + restart** when the speaker keeps talking; a new question mid-answer → old card `answer.cancelled(reason="interrupted")`;
   - answers grounded only in resume + job description + story bank; weak grounding → `not_sure: true` and a generic framework, never invented employers, numbers or skills;
   - **Target: first visible words ≲ 1 s p50** (streaming STT, §2.2); cancel/restart proven by tests.
3. **Gates (server-side):** `voice_prep` for Practice; `live_copilot` (off by default; admin per user) **plus** a global flag `live_copilot_enabled` **plus** the user's **first-use acknowledgement** for the copilot. Missing any → `403` with a reason code; the socket is refused before any audio is read.
4. **Privacy:** raw audio and screenshots are **never stored** (memory only, dropped after transcription / the one LLM call); transcripts and answers **encrypted at rest** (`key_store.seal`); retention `VOICE_TRANSCRIPT_RETENTION_DAYS` (90); `DELETE /voice/sessions/{id}` and `DELETE /voice/history` wipe everything; logs never contain transcript text.
5. **Cost control:** every STT second, TTS character and LLM token goes to the usage ledger with the session id; a **per-session cost cap** (Practice `$0.50`, Copilot `$1.00`, env) and a max length (Practice 45 min, Copilot 90 min). Hitting the cap switches to free models; with none left the session ends with a clear `error{code:"session_cost_cap"}`.
6. **Story bank:** CRUD + "draft stories from my resume" (AI drafts, the user approves before they are used).
7. **Transparency:** visible `status` events (`listening` / `paused` / `ended`) the client must render; no hide-from-screen-share, stealth or detection-evasion anything (CLAUDE.md §7.10, §16 do-not list).
8. Offline tests with fake STT/TTS/LLM providers cover all of the above; a benchmark script measures real p50/p95 per stage. The existing 311 tests stay green.

---

## 2. Design

### 2.1 One streaming engine, two modes

```
WS (FastAPI) ─▶ VoiceSession (one asyncio object per socket; owns every task)
                 ├─ AudioIn: bounded queue per channel (drop oldest, ≤ 2 s)
                 ├─ STT stream per channel (provider adapter) ─▶ partial / final transcripts
                 ├─ TurnManager (Practice: end-of-answer, barge-in) │ QuestionGate (Copilot)
                 ├─ Context: job description, resume, story-bank top 3, last 60 s, prior Q&A
                 ├─ LLM stream via router (fast first; strong on expand / hard)
                 ├─ TTS stream (Practice only; or text for browser TTS)
                 └─ Recorder: writes voice_turns (sealed) in short DB sessions; ledger; latency marks
```

- **Plain asyncio** (no Pipecat). Every generation is an `asyncio.Task` stored on the session; cancel = `task.cancel()` + `answer.cancelled`. `VoiceSession.close()` cancels everything in a `finally`, so a dropped socket can't leak provider streams.
- **DB use:** no transaction is held for the socket's life (Supabase pooler is transaction-mode). Turns are written with short `SessionLocal()` blocks as they finish.
- **Runs in the API process**, not Celery. Celery only runs the after-session work (`voice_finalize`, default queue): evaluation, copilot summary, purge.
- **Reconnect:** turns are persisted as they finish and the session row keeps `state`; a reconnect within `VOICE_RESUME_SECONDS` (60) to the same session id gets the last cards back (`session.resumed` with the turns) and continues. After that the session is ended and finalized.

### 2.2 Providers (free first, all behind the registry) `[verify names, limits and prices before building]`

The router (CLAUDE.md §7.9) gains tasks `stt`, `tts`, `copilot_answer` (fast), `copilot_expand` (strong), `question_gate` (tiny, only when the rules are unsure); `interview_chat` / `interview_eval` already exist. Providers are `ai_models` rows like today (admin adds them; users choose per task; BYOK works; Free tools only respected).

| Layer | Free default (no paid key) | Paid upgrade (needs a key) | Notes |
|---|---|---|---|
| **STT** | **Groq Whisper `whisper-large-v3-turbo`** (free key), per utterance after server VAD end-of-speech | **Deepgram Nova-3 streaming** (true partials, endpointing, diarization; new accounts get free credit); **OpenAI** `gpt-4o-transcribe` / `whisper-1` per utterance (same adapter as Groq) | Groq has **no partials** and a low free rate limit, so speculative start is off and copilot latency is ≈ 1.2–1.8 s. The ≲ 1 s copilot target needs Deepgram (or another streaming STT such as AssemblyAI Universal-Streaming, added later). |
| **VAD** | `webrtcvad-wheels` on the server (end-of-speech for Groq; barge-in) | Deepgram's own endpointing when it is the STT | New dependency, small, prebuilt wheels incl. Windows. |
| **TTS (Practice)** | **Browser speech** (`speechSynthesis`): the server streams sentence-sized text, the client speaks it — zero cost, lowest latency | **Deepgram Aura**, **ElevenLabs**, or **OpenAI** `gpt-4o-mini-tts` (server sends audio chunks) | "browser" is a registry row with `tier=free`, so it is still chosen through the registry. Local Piper is possible later (CPU on the VM). |
| **Fast LLM** | **Groq `gpt-oss-120b`** (free key) → Gemini Flash → **OpenRouter** (last fallback) | **Claude Haiku 5.5**; **OpenAI** (ChatGPT models, e.g. a mini model) | Streaming adapters needed (§2.3). |
| **Strong LLM** (expand / hard / evaluation) | Gemini (largest free) → Groq → **OpenRouter** (last fallback) | **Claude Sonnet 5.5** (or Opus 5.5); **OpenAI** (ChatGPT models) | Post-session evaluation can use the strong model; it is not latency-bound. |
| **Vision** (copilot screenshot) | Gemini Flash (free key) → **OpenRouter** vision model | Claude Sonnet 5.5; OpenAI | Only when the user sends an image (§2.6). |
| **Retrieval** | Keyword/BM25-style ranking over the user's resume chunks, job description and story bank (small per user, in memory) | — | No pgvector in P7 (decision 7). |

**OpenRouter as the fallback (owner request, 2026-10-20).** OpenRouter already works today through the `openai_compatible` provider (`base_url https://openrouter.ai/api/v1`, key scope `openrouter` — `model_registry` derives it from the host). P7 adds:
- the owner's OpenRouter key stored once as a **platform key** in Admin → Platform keys (scope `openrouter`; or as a BYOK key per user) — never in env files, the repo or logs;
- `ai_models` rows for OpenRouter models on the voice tasks (`interview_chat`, `interview_eval`, `copilot_answer`, `copilot_expand`, `question_gate`, vision) with the **lowest priority**, so the router uses them only when the earlier models fail, are rate-limited (429) or have an open circuit breaker. Rows for OpenRouter `:free` models are `tier=free` (allowed under Free tools only); paid OpenRouter models are `tier=paid` and use the account's credits;
- the fallback switch happens **before the first token** (§2.3), so a slow or failing primary costs at most the first-token timeout.
OpenRouter has no speech-to-text/text-to-speech endpoint, so it is an LLM fallback only.

**ChatGPT (OpenAI) models, ready but not required.** Nothing in P7 is tied to one vendor:
- text: an OpenAI row is `openai_compatible` + `base_url https://api.openai.com/v1` (key scope `openai`) — works for every task once an admin adds the model and a key; no code change;
- speech: the Whisper STT adapter is written against the **OpenAI-compatible `/audio/transcriptions` API** (Groq uses the same API), and a matching `/audio/speech` TTS adapter is added, so OpenAI STT/TTS are a registry row + key away;
- later, not P7: OpenAI's Realtime API (streaming speech in/out) as another `STTProvider`/`TTSProvider` adapter.

**What needs a paid key:** Deepgram (streaming STT and/or Aura TTS), ElevenLabs, Claude, OpenAI, and paid OpenRouter models. Everything works without them, slower and with browser voices. `GET /voice/providers` tells the client which path is active and the expected latency, so the UI is honest ("Free mode: answers in about 1.5 s").

### 2.3 Streaming adapters
- New `app/providers/voice/`: `STTProvider.transcribe(frames) -> AsyncIterator[Transcript(partial|final, text, start_ms, end_ms, words?)]`, `TTSProvider.speak(text_stream) -> AsyncIterator[bytes]` (Practice, server TTS only); adapters `OpenAICompatibleSTT` (VAD-segmented, WAV in memory, multipart POST to `{base_url}/audio/transcriptions` via `httpx.AsyncClient`; serves Groq Whisper now and OpenAI later), `DeepgramSTT` (WS via `websockets`, reconnect once), `BrowserTTS` (no-op marker), `OpenAICompatibleTTS` (`{base_url}/audio/speech`, streamed), `DeepgramAuraTTS`, `ElevenLabsTTS`.
- `LLMProvider` gains **`async def stream(spec, *, api_key, system, user, max_tokens) -> AsyncIterator[str]`** for Groq / OpenAI-compatible (SSE — this one adapter covers OpenRouter and OpenAI), Anthropic (messages stream) and Gemini (`generate_content_stream`); Hugging Face falls back to `complete()` in a thread, sent as one chunk.
- `ai_router.stream(task, …)` mirrors today's resolution order + circuit breaker, but **fails over only before the first token** (after that the card is marked `answer.cancelled(reason="provider_error")` with retry). Per-call timeouts: first token `VOICE_FIRST_TOKEN_TIMEOUT_MS` (2500).
- Keys are resolved inside the session by `key_ref` (never sent to the client or logged).

### 2.4 Practice mode
- **Turn manager:** end of answer = final transcript + `VOICE_END_OF_TURN_MS` (700) silence (shorter when the sentence ends cleanly). Barge-in: user speech while the interviewer is speaking → stop TTS (`tts.stop`), mark the interviewer turn `interrupted`, listen.
- **Interviewer prompt** (`interview_chat`): job description (≤ 3,000 chars), resume summary, type, difficulty, questions asked so far, the last answer → returns the next utterance (follow-up or next question) streamed sentence by sentence; first sentence goes to TTS as soon as it ends. Prompt-injection guard on job text as in other prompts.
- **Delivery metrics** computed from word timestamps (Deepgram) or utterance duration (Groq): words per minute, filler count (`um, uh, like, you know, basically, actually…` list per language), long pauses. Stored as numbers on the turn, not audio.
- **Evaluation** (`voice_finalize` → `interview_eval`, JSON): per answer `{accuracy, relevance, structure, clarity, delivery}` 1–5, STAR parts present, strengths, improvements, model answer; session summary + skill areas. **Grounding check** (existing `services/grounding.py`): employer names, numbers and skills in model answers must appear in the resume/story bank, else rewritten generic ("[your result]").
- **Progress:** `GET /voice/progress` averages per skill area and interview type over time; "drill weak questions" = a new Practice session seeded with the lowest-scored questions.

### 2.5 Live Copilot
- **Channels:** `interviewer` (tab audio on web) and `user` (mic). Only `interviewer` finals/partials feed the gate; `user` text is context ("already said") so answers don't repeat it.
- **Mic-only fallback** (no tab audio): one channel labelled `mixed`. With Deepgram, diarization splits speakers, but which speaker is the user is **not guessed**: the user marks it once ("that was me" on a card) — until then every question-like utterance is treated as the interviewer's. With Groq (no diarization) everything goes to the gate and **Answer this** is the main control. The UI says detection is less precise.
- **Question gate** (`services/question_gate.py`, < 50 ms): rules first — `?`, wh-/aux-verb openers, "tell me / walk me / describe / explain / how would you", imperative-interview phrases, follow-up cues ("why", "elaborate", "what about…", "and then?") within 30 s of the last card, multi-part ("and also", "two things") → `type` (behavioral, technical, coding, system_design, hr, situational, intro) + `is_followup`. Non-questions (greetings, "can you hear me", fillers) are rejected. Only an "unsure" band calls `question_gate` on the tiny model (cached per text, 300 ms timeout, default reject).
- **Speculative start** (streaming STT only): partial passes the gate with a terminal cue → start generation; if more interviewer words arrive within `VOICE_SPECULATIVE_GRACE_MS` (400) that change the question → cancel + restart with the full text (not shown as a new card). With Groq the gate runs on finals only.
- **Answer:** `copilot_answer` (fast) with style (bullets / short script / STAR), length, language, top-3 retrieved chunks, last 60 s transcript, the parent Q&A for follow-ups. `expand` / gate `hard` → `copilot_expand` (strong). `regenerate` reruns; `shorter` = expand with a "≤ 40 words" instruction. `not_sure` when the best retrieval score is under `VOICE_GROUNDING_MIN` → generic framework + the tag.
- **Interrupts:** a new detected question while a stream runs → cancel it, `answer.cancelled{reason:"interrupted"}`, start the new one. Only one generation per session at a time (bounded cost).
- **Hard cases on protocol:** `control.answer_now` answers the last 20 s of interviewer (or mixed) text; `dismiss` marks the card dismissed (kept in history as dismissed).
- After **Stop**: `voice_finalize` builds the summary, weak topics and "questions to prepare", stored as session feedback; Practice can be seeded from them.

### 2.6 WebSocket protocol details (beyond CLAUDE.md §7.10)
- **Auth without URL secrets:** `POST /voice/sessions` returns `{session, stream_token}` — a JWT (60 s, single-use `jti` in Redis, bound to user + session id). The client opens the socket and its **first text message** is `{"type":"auth","token":…}`; no auth within 5 s → close `4401`. Then the gateway re-checks `is_active`, entitlement, flag and acknowledgement (copilot), and the per-user limit of **one live voice session**.
- **Audio frames are binary:** `byte 0` channel (`0` interviewer, `1` user, `2` mixed), `bytes 1–4` uint32 ms since session start, then 16 kHz mono PCM16 (20–100 ms). Larger frames → close `1009`. Every other message is JSON text with `type`.
- **Practice events** (copilot events as in §7.10): client `audio.frame`, `control{start|pause|resume|end|repeat|skip|barge_in}`; server `status`, `transcript.partial/final`, `interviewer.delta` (text, sentence-flushed), `interviewer.audio` (binary, server TTS only, prefixed byte `9`), `turn.done`, `session.ended{feedback_pending:true}`, `error{code,message,retryable}`.
- **Screenshot** (copilot): `{"type":"screenshot","image":"data:image/png;base64,…"}` ≤ 2 MB, only sent when the user acts (web: paste or pick an image — decision 11); used for one vision call, then dropped. Never written to DB, disk or logs.
- **Heartbeats:** server `ping` every 15 s; a client silent for 30 s → session paused, 60 s → ended.
- Close codes: `4401` auth, `4403` gate (entitlement / flag / acknowledgement), `4409` another live session, `4429` rate limit, `4402` cost cap.

### 2.7 Acknowledgement (first use, mandatory)
- `voice_settings.copilot_ack_version` + `copilot_ack_at`. Current text lives in config as `COPILOT_ACK_VERSION` (e.g. `2026-10`) + the text returned by `GET /voice/settings` (wording from CLAUDE.md §7.10, including the recommended uses).
- `POST /voice/copilot/acknowledge {version}` records it (audit log entry). Creating a copilot session or opening its socket without the **current** version → `403 copilot_ack_required`. Changing the text = new version = asked again.

### 2.8 Data model (migration `0012`)

| Table | Columns |
|---|---|
| `voice_sessions` | id, user_id (idx), mode (`mock` \| `copilot`), state (`created/active/paused/ended/failed`), job_id?, resume_id?, config JSON (type, difficulty, length, style, language, audio sources), providers JSON (stt/tts/llm used), started_at, ended_at, duration_s, cost_usd, cost_cap_usd, latency JSON (p50/p95 per stage), summary_sealed, purge_at |
| `voice_turns` | id, session_id (idx), user_id, seq, speaker (`interviewer/user/mixed/assistant`), kind (`question/answer/suggestion/utterance`), text_sealed, question_id, parent_id (follow-ups), qtype, is_followup, state (`shown/interrupted/cancelled/dismissed`), not_sure, start_ms, end_ms, metrics JSON (wpm, fillers, pauses), latency JSON |
| `voice_feedback` | id, session_id, turn_id? (null = session level), user_id, scores JSON, skill_area, strengths, improvements, model_answer_sealed, star JSON |
| `story_bank` | id, user_id, title, situation, task, action, result, skills JSON, origin (`user/ai`), approved, uses; no embedding column yet |
| `voice_settings` | user_id (pk), copilot_ack_version, copilot_ack_at, answer_style, answer_length, language, tts_voice, default_sources |

Retention: `purge_at = ended_at + VOICE_TRANSCRIPT_RETENTION_DAYS`; daily `purge_voice_data` (maintenance queue) deletes sessions + turns + feedback. Story bank is kept until the user deletes it (it's profile data).

### 2.9 Endpoints (CLAUDE.md §9, completed)

| Endpoint | Purpose |
|---|---|
| `GET /voice/providers` | active STT/TTS/LLM path for the user, free/paid, expected latency, whether tab-audio diarization is available |
| `GET/PUT /voice/settings` | style, length, language, TTS voice, ack text + version + whether acknowledged |
| `POST /voice/copilot/acknowledge` | record the acknowledgement (current version) |
| `POST /voice/sessions` | `{mode, job_id?, role?, config}` → `{session, stream_token}`; gates, quota (`quota_per_day` = sessions/day), one live session |
| `POST /voice/sessions/{id}/token` | fresh stream token for reconnect |
| `WS /voice/sessions/{id}/stream` | Practice |
| `WS /voice/copilot/{id}/stream` | Live Copilot (§7.10 protocol) |
| `GET /voice/sessions/{id}` | transcript (unsealed for the owner), cards, metrics |
| `GET /voice/sessions/{id}/feedback` | evaluation / copilot summary (`pending` until `voice_finalize` finishes) |
| `GET /voice/history?mode=&limit=&offset=` | list with scores, duration, cost |
| `GET /voice/progress` | per-skill-area trend |
| `DELETE /voice/sessions/{id}`, `DELETE /voice/history` | wipe one / all |
| `GET/POST /story-bank`, `PUT/DELETE /story-bank/{id}`, `POST /story-bank/draft-from-resume` (202 + task_id), `POST /story-bank/{id}/approve` | story bank |
| `GET /admin/voice/health` | sessions today, p50/p95 per stage per provider, error rate, spend |

`POST /voice/copilot/{id}/expand` from §9 is **not built**: expand is the WS `control` message (one path; CLAUDE.md §9 updated) — decision 12.

### 2.10 Limits and config
`RATE_LIMIT_VOICE_SESSIONS` (20/day), one live session per user, `VOICE_PRACTICE_MAX_MINUTES` (45), `VOICE_COPILOT_MAX_MINUTES` (90), `VOICE_PRACTICE_COST_CAP_USD` (0.50), `VOICE_COPILOT_COST_CAP_USD` (1.00), `VOICE_TRANSCRIPT_RETENTION_DAYS` (90), `VOICE_RESUME_SECONDS` (60), `VOICE_END_OF_TURN_MS` (700), `VOICE_SPECULATIVE_GRACE_MS` (400), `VOICE_FIRST_TOKEN_TIMEOUT_MS` (2500), `VOICE_GROUNDING_MIN`, `VOICE_MAX_FRAME_MS` (100), `VOICE_AUDIO_QUEUE_MS` (2000), `COPILOT_ACK_VERSION`, flag `live_copilot_enabled` (default true; the entitlement stays off per user).

### 2.11 Observability
Latency marks per turn: end-of-speech → final, gate, first token, first visible / first spoken, total; stored on the turn and rolled into session p50/p95; structured logs carry `session_id`, never text. `app/scripts/voice_bench.py` plays synthetic question WAVs (generated once with TTS, committed as test fixtures — not user audio) through the real socket with the configured providers and prints p50/p95 per stage — this is how the §15 targets are checked.

---

## 3. Steps

Build **P7a Practice** first (steps 1–7), then **P7b Copilot** (8–12); each can be committed separately.

1. Migration `0012` (§2.8), models + Pydantic schemas; `voice_settings`; flag `live_copilot_enabled`; router tasks `stt`, `tts`, `copilot_answer`, `copilot_expand`, `question_gate`.
2. Streaming LLM: `stream()` on Groq / OpenAI-compatible / Anthropic / Gemini adapters; `ai_router.stream` with failover-before-first-token, ledger, circuit breaker.
3. Voice providers: STT/TTS interfaces, OpenAI-compatible STT (Groq Whisper now) + webrtcvad segmenter, Deepgram STT, browser TTS marker, OpenAI-compatible / Deepgram Aura / ElevenLabs TTS; fake providers for tests; seed `ai_models` rows (disabled until a key exists, except browser TTS), including **OpenRouter fallback rows at the lowest priority** for the voice LLM tasks and disabled-by-default **OpenAI** rows (text, STT, TTS) ready to switch on.
4. WS gateway: first-message auth with single-use stream token, gates, binary frame parser, bounded queues, heartbeats, `VoiceSession` lifecycle + reconnect/resume, cost meter + cap, close codes.
5. Practice turn manager, interviewer prompt, barge-in, delivery metrics, turn recorder (sealed).
6. `voice_finalize` (evaluation + grounding), feedback, history, progress, delete endpoints, `purge_voice_data`.
7. Story bank CRUD + draft-from-resume + approve; retrieval ranking used by Practice model answers.
8. Acknowledgement endpoint + enforcement on create and socket.
9. Question gate (rules + tiny-model band) with a labelled set of ~150 utterances (question / non-question / follow-up / multi-part, English + Hinglish) as a CI eval (≥ 0.9 precision on "question").
10. Copilot pipeline: channels, speculative start + cancel/restart, interrupts, answer styles, expand/shorter/regenerate/dismiss/answer_now, `not_sure`, screenshot → vision (memory only), mixed-channel mode.
11. `voice_finalize` copilot summary; `GET /voice/providers`, `GET /admin/voice/health`; `voice_bench.py`.
12. Tests (fake providers, fake clock): auth/gates/ack (incl. a revoked entitlement mid-session closes the socket), one-live-session, frame parser and queue drop, Practice turn + barge-in, speculative cancel/restart, interrupt on new question, follow-up threading, grounding (no invented employers/numbers), cost cap switch-then-end, reconnect resume, **no audio or image bytes in DB/logs** (scan rows and captured logs), encryption of text columns, purge, delete. CLAUDE.md §6.13 (status), §7.10, §8–§10, §13.

**New env:** the `VOICE_*` / `COPILOT_*` keys in §2.10; provider keys go through the existing key store (`GROQ`, `DEEPGRAM`, `ELEVENLABS`, `ANTHROPIC`, `GEMINI` platform keys or BYOK).
**New dependencies:** `webrtcvad-wheels`; `websockets` (already pulled in by `uvicorn[standard]` — confirm, else add).
**Operations:** nginx must pass WebSocket upgrades on `/api/v1/voice/`; voice sockets need **sticky sessions** if uvicorn runs more than one worker (or run one worker for now); read timeout ≥ 120 s.

---

## 4. Risks

| Risk | Mitigation |
|---|---|
| Free STT (Groq) has no partials and a tight free rate limit → copilot misses ≲ 1 s, long sessions hit 429 | Honest "free mode ≈ 1.5 s" from `GET /voice/providers`; VAD sends only speech; 429 → `error{retryable}` + Answer this; Deepgram key unlocks the target (decision 1) |
| Latency targets depend on region (VM in Oracle region vs providers in US) | `voice_bench.py` measures p50/p95 from the VM; targets reported per provider; acceptance run with the recommended paid set noted separately |
| OpenRouter `:free` models are rate-limited and can disappear; paid OpenRouter models spend credits silently | Lowest priority (used only on failure); circuit breaker; spend shows per model in the usage ledger and `GET /admin/voice/health`; per-session cost cap applies to OpenRouter too |
| Runaway cost (long calls, repeated regenerate) | One generation at a time, per-session cap + max minutes, daily session quota, ledger per session, Free tools only respected |
| Copilot used where AI help is forbidden | Mandatory versioned acknowledgement, visible status, off by default per user + global flag; no stealth features (and tests assert no such options exist in settings) |
| Transcripts are sensitive | Sealed columns, 90-day purge, delete endpoints, no text in logs, no audio/images ever persisted |
| Socket leaks / zombie provider streams | One session object owns all tasks; `finally` cancels; heartbeat timeouts; tests with fake providers that record open streams |
| Mic-only mode answers the user's own speech | Gate rules + "that was me" speaker marking (Deepgram) + Answer this; UI says it's less precise |
| Multi-worker uvicorn breaks reconnect | Session state in DB, resume by id; sticky sessions or one worker (ops note) |
| Prompt injection via job description / spoken text | Same guard as other prompts; answers never execute instructions from the transcript |

---

## 5. Not in P7
- **Desktop client** (Tauri): system-audio loopback, always-on-top overlay, global hotkeys — **P7.1 / F7.1** (decision 5).
- pgvector embeddings for the story bank (P8 if keyword ranking isn't enough).
- Local Piper TTS / faster-whisper STT on the VM; AssemblyAI adapter.
- Calendar-based "prep for this interview" reminders (Appendix B).

---

## 6. Owner decisions needed before building

1. **STT:** free default **Groq Whisper** (per utterance, server VAD; copilot ≈ 1.5 s, no speculative start); **Deepgram Nova-3 streaming as the paid upgrade** and the provider used to prove the ≲ 1 s copilot target. _Recommended: yes — and add a Deepgram key (new-account credit covers testing) when you want the fast path._
2. **TTS for Practice:** **browser speech by default** (free, instant), Deepgram Aura / ElevenLabs as optional paid voices through the registry. _Recommended: yes._
3. **LLMs:** fast = Groq `gpt-oss-120b` → Gemini Flash (free) → **OpenRouter**; strong/expand/evaluation = Claude Sonnet 5.5 when a key exists, else the largest free model → **OpenRouter**. _Recommended: yes._
   - 3a. **OpenRouter = last fallback** for every voice LLM task, using your key as a platform key (Admin → Platform keys, scope `openrouter`). Which OpenRouter models: _Recommended: a `:free` model for fast answers (counts as free) and one paid model for strong/expand, so a failure on the strong path still gets a good answer — you pick the exact models in Admin → AI models._
   - 3b. **ChatGPT (OpenAI) ready:** OpenAI text, STT and TTS rows seeded **disabled**; adding an OpenAI key and switching a row on is all it takes, no code change. _Recommended: yes._
4. **Order:** one P7 plan, built as **P7a Practice** then **P7b Copilot**, committed separately. _Recommended: yes._
5. **Desktop (Tauri) client later** (P7.1 / F7.1); P7 ships the web copilot only (tab audio + mic, in-page overlay). _Recommended: yes — web covers Meet/Zoom-web/Teams-web; desktop needs Rust, signing and OS loopback per platform._
6. **Web interviewer audio:** tab audio via `getDisplayMedia` (Chrome/Edge) as the `interviewer` channel + mic as `user`; **mic-only `mixed` fallback** with Deepgram diarization when available, otherwise gate + Answer this. _Recommended: yes._
7. **Story-bank retrieval by keyword ranking** in P7, no pgvector yet. _Recommended: yes (a few dozen chunks per user)._
8. **Transcript retention 90 days**, encrypted; per-session and delete-all endpoints. _Recommended: yes (answers CLAUDE.md §17 Q8 for transcripts)._
9. **Cost caps:** $0.50 per Practice session, $1.00 per Copilot session, 45 / 90 min max, 20 sessions/day, one live session at a time; at the cap switch to free models, then end. _Recommended: yes (answers §17 Q6 budget)._
10. **Acknowledgement:** versioned text, stored with timestamp, audited, re-asked when the text changes, enforced on session create **and** socket open. _Recommended: yes._
11. **Copilot screenshot on web:** the user pastes or picks an image ("Send screenshot") — one vision call, never stored; no automatic screen capture. _Recommended: yes (global screenshot hotkey comes with the desktop client)._
12. **Socket auth by first message** with a single-use 60 s token (no token in the URL); expand/shorter/regenerate only as WS `control` messages (drop `POST /voice/copilot/{id}/expand` from §9). _Recommended: yes._
13. **Global kill switch** `live_copilot_enabled` (feature flag) in addition to the per-user `live_copilot` entitlement that stays **off by default**. _Recommended: yes._
14. **Deployment:** voice sockets in the API process; one uvicorn worker (or sticky sessions) + nginx WS upgrade on the VM. _Recommended: yes for now._
