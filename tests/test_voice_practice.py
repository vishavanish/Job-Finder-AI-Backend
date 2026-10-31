"""
P7a — Voice Practice mode: streaming router, speech registry rows, VAD,
the Practice socket (auth, gates, typed and spoken answers, follow-ups,
barge-in, cost cap, one live session), evaluation, history, retention and
the story bank. Fake speech and AI providers; no network.
"""
from __future__ import annotations

import array
import json
import math
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.db import SessionLocal
from app.models.ai import AiModel
from app.models.voice import StoryBankEntry, VoiceFeedback, VoiceSession, VoiceTurn
from app.providers.voice.base import Transcript
from app.providers.voice.vad import Segmenter, SpeechStart, Utterance
from app.services import (
    ai_router, entitlements, practice_session, story_bank, voice_eval, voice_speech, voice_store,
)

API = "/api/v1"


# ---------------------------------------------------------------- fakes

class Script:
    """Scripted interviewer replies for the fake router stream."""

    def __init__(self, replies, *, cost=0.0, block_after_first=False, fail_when_free_only=False):
        self.replies = list(replies)
        self.cost = cost
        self.block_after_first = block_after_first
        self.fail_when_free_only = fail_when_free_only
        self.calls: list[dict] = []

    def stream(self, db, *, user_id, task, system, user_prompt, outcome, max_tokens=800, ref=None,
               free_only=False, timeout=None, cancel=None, images=None, first_token_timeout=None):
        self.calls.append({"task": task, "prompt": user_prompt, "free_only": free_only, "system": system})
        if self.fail_when_free_only and free_only:
            raise ai_router.NoModelAvailable(task, ["test: paid model skipped"])
        text = self.replies.pop(0) if self.replies else "[END] Thank you."
        outcome.first_token_ms = 5
        sentences = [s for s in text.split("|")]
        for i, piece in enumerate(sentences):
            yield piece
            if self.block_after_first and i == 0:
                while cancel is not None and not cancel.is_set():
                    time.sleep(0.01)
                outcome.cancelled = True
                return
        outcome.text = "".join(sentences)
        outcome.cost_usd = self.cost


class FakeSTT:
    def __init__(self, texts):
        self.texts = list(texts)
        self.seen_bytes = 0

    async def transcribe(self, pcm, *, language=None):
        self.seen_bytes += len(pcm)
        return Transcript(text=self.texts.pop(0) if self.texts else "")


@pytest.fixture
def script(monkeypatch):
    holder = {}

    def install(replies, **kw):
        s = Script(replies, **kw)
        monkeypatch.setattr(ai_router, "stream", s.stream)
        holder["s"] = s
        return s
    return install


@pytest.fixture
def stt(monkeypatch):
    fake = FakeSTT([])
    choice = voice_speech.SpeechChoice("fake-stt", "groq", "whisper-fake", "Fake Whisper", "free", None, "", "none")
    orig = voice_speech.resolve

    def resolve(db, *, user_id, task, free_only=False):
        if task == "stt":
            return [choice], []
        return orig(db, user_id=user_id, task=task, free_only=free_only)

    monkeypatch.setattr(voice_speech, "resolve", resolve)
    monkeypatch.setattr(voice_speech, "make_stt", lambda c: fake)
    monkeypatch.setattr(voice_speech, "_ledger", lambda *a, **k: 0.0)
    # Sine-wave "speech": use the energy VAD so the test doesn't depend on webrtcvad's voice model.
    monkeypatch.setattr(practice_session, "Segmenter", lambda **kw: Segmenter(**{**kw, "use_webrtc": False}))
    return fake


@pytest.fixture(autouse=True)
def _speech_to_text(stt):
    """Every socket test has a (fake) speech-to-text model."""
    return stt


def tone(ms: int, ts: int, *, loud=True) -> bytes:
    n = ms * 16
    samples = array.array("h", (int(8000 * math.sin(i / 5)) if loud else 0 for i in range(n)))
    return bytes([1]) + ts.to_bytes(4, "little") + samples.tobytes()


def start(client, headers, **body):
    resp = client.post(f"{API}/voice/sessions", json={"length": 1, **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_closed(ws):
    for _ in range(20):
        try:
            ws.receive_json()
        except WebSocketDisconnect:
            return
    raise AssertionError("socket did not close")


def recv_until(ws, kind, limit=60):
    seen = []
    for _ in range(limit):
        try:
            msg = ws.receive_json()
        except WebSocketDisconnect as e:
            raise AssertionError(f"closed {e.code} waiting for {kind}; got {[m.get('type') for m in seen]} {seen[-2:]}")
        seen.append(msg)
        if msg["type"] == kind:
            return msg, seen
    raise AssertionError(f"no {kind} in {[m['type'] for m in seen]}")


def connect(client, created):
    ws = client.websocket_connect(created["stream_path"])
    return ws


def auth(ws, created):
    ws.send_text(json.dumps({"type": "auth", "token": created["stream_token"]}))


# ---------------------------------------------------------------- registry and router

def test_speech_rows_never_serve_text_and_text_rows_never_serve_speech():
    db = SessionLocal()
    try:
        whisper = db.query(AiModel).filter(AiModel.model_id == "whisper-large-v3-turbo").one()
        browser = db.query(AiModel).filter(AiModel.provider == "browser").one()
        gemini = db.query(AiModel).filter(AiModel.provider == "gemini").first()
        openrouter = db.query(AiModel).filter(AiModel.key_scope == "openrouter").one()
        assert ai_router.serves(whisper, "stt") and not ai_router.serves(whisper, "score")
        assert ai_router.serves(browser, "tts") and not ai_router.serves(browser, "interview_chat")
        assert ai_router.serves(gemini, "interview_chat") and not ai_router.serves(gemini, "stt")
        # OpenRouter is the last fallback for every text task, off until a key is stored.
        assert openrouter.priority == 900 and not openrouter.enabled_global
        assert ai_router.serves(openrouter, "copilot_answer")
        assert db.query(AiModel).filter(AiModel.key_scope == "openai", AiModel.enabled_global.is_(True)).count() == 0
    finally:
        db.close()


def test_router_stream_fails_over_before_the_first_chunk_only(monkeypatch, make_user):
    user_id, _ = make_user()
    a = AiModel(id="m-a", provider="groq", model_id="a", display_name="A", tier="free", tasks=[], key_scope="groq",
                cost_per_1k_in=0, cost_per_1k_out=0, priority=1, enabled_global=True, consecutive_failures=0)
    b = AiModel(id="m-b", provider="groq", model_id="b", display_name="B", tier="free", tasks=[], key_scope="groq",
                cost_per_1k_in=0, cost_per_1k_out=0, priority=2, enabled_global=True, consecutive_failures=0)
    monkeypatch.setattr(ai_router, "resolve_chain", lambda db, **kw: ([a, b], []))
    monkeypatch.setattr(ai_router, "resolve_key", lambda db, **kw: ("k", "platform"))
    monkeypatch.setattr(ai_router, "get_adapter", lambda provider: object())

    def stream_text(adapter, spec, **kw):
        if spec.model_id == "a":
            raise ai_router.LLMError("HTTP 429 from model endpoint")
        yield "Hello "
        yield "there."

    monkeypatch.setattr(ai_router, "stream_text", stream_text)
    monkeypatch.setattr(ai_router, "_ledger", lambda db, **kw: 0.0)
    outcome = ai_router.StreamOutcome()
    db = SessionLocal()
    try:
        chunks = list(ai_router.stream(db, user_id=user_id, task="interview_chat", system="s", user_prompt="u",
                                       outcome=outcome))
    finally:
        db.close()
    assert chunks == ["Hello ", "there."] and outcome.model is b and outcome.text == "Hello there."
    assert any("A:" in x for x in outcome.attempts)

    def broken_mid_stream(adapter, spec, **kw):
        yield "Half"
        raise ai_router.LLMError("connection reset")

    monkeypatch.setattr(ai_router, "stream_text", broken_mid_stream)
    db = SessionLocal()
    try:
        with pytest.raises(ai_router.LLMError):
            list(ai_router.stream(db, user_id=user_id, task="interview_chat", system="s", user_prompt="u",
                                  outcome=ai_router.StreamOutcome()))
    finally:
        db.close()


def test_free_only_skips_paid_models(make_user):
    user_id, _ = make_user()
    db = SessionLocal()
    try:
        paid = AiModel(provider="anthropic", model_id="claude-x", display_name="Paid", tier="paid", tasks=[],
                       key_scope="anthropic", enabled_global=True)
        verdict = ai_router.access(db, None, paid, free_only=True)
        assert not verdict.allowed and "cost cap" in verdict.reason
    finally:
        db.close()


def test_vad_cuts_utterances_and_ignores_short_noise():
    seg = Segmenter(end_silence_ms=300, min_speech_ms=200, use_webrtc=False)
    events = []
    ts = 0
    for ms, loud in ((100, False), (60, True), (300, False), (600, True), (400, False)):
        events += seg.feed(tone(ms, ts, loud=loud)[5:], ts)
        ts += ms
    utts = [e for e in events if isinstance(e, Utterance)]
    assert len(utts) == 1  # the 60 ms blip never became speech
    assert any(isinstance(e, SpeechStart) for e in events)
    assert 400 <= utts[0].start_ms <= 470 and 1050 <= utts[0].end_ms <= 1100


# ---------------------------------------------------------------- REST

def test_settings_and_copilot_ack_text(client, make_user):
    _, headers = make_user()
    body = client.get(f"{API}/voice/settings", headers=headers).json()
    assert body["copilot_acknowledged"] is False and "prohibit AI assistance" in body["copilot_ack_text"]
    assert body["transcript_retention_days"] == 90
    body = client.put(f"{API}/voice/settings", json={"answer_style": "star", "language": "hi"}, headers=headers).json()
    assert body["answer_style"] == "star" and body["language"] == "hi"


def test_providers_say_what_is_missing(client, make_user):
    _, headers = make_user()
    body = client.get(f"{API}/voice/providers", headers=headers).json()
    assert body["tts"]["kind"] == "browser"
    assert body["stt"]["name"] == "Fake Whisper"
    assert body["ready"] is False and "interviewer" in body["reason"]  # no LLM key in tests


def test_voice_prep_entitlement_is_enforced(client, make_user):
    user_id, headers = make_user()
    db = SessionLocal()
    try:
        entitlements.set_override(db, user_id, "voice_prep", enabled=False, quota_per_day=None, max_level=None,
                                  expires_at=None)
        db.commit()
    finally:
        db.close()
    assert client.post(f"{API}/voice/sessions", json={}, headers=headers).status_code == 403


def test_live_copilot_stays_off_by_default(make_user):
    user_id, _ = make_user()
    db = SessionLocal()
    try:
        assert entitlements.effective(db, user_id, "live_copilot").enabled is False
    finally:
        db.close()


# ---------------------------------------------------------------- the socket

def test_socket_needs_a_valid_single_use_token(client, make_user, script):
    _, headers = make_user()
    created = start(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "nope"}))
        assert ws.receive_json()["code"] == "auth_failed"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == practice_session.CLOSE_AUTH
    script([])
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        assert ws.receive_json()["type"] == "status"
        # End before leaving: the test client cancels a server still saving a dropped session.
        ws.send_text(json.dumps({"type": "control", "action": "end"}))
        recv_until(ws, "session.ended")
    with client.websocket_connect(created["stream_path"]) as ws:  # the same token again
        auth(ws, created)
        assert ws.receive_json()["code"] == "auth_failed"


def test_typed_interview_with_follow_up_and_closing(client, make_user, script, dispatched):
    user_id, headers = make_user()
    s = script(["[Q] Hi, I'm Sam.| Tell me about a project you led.",
                "[F] What was the result?",
                "[END] Thank you, your feedback is being prepared."])
    created = start(client, headers, interview_type="behavioral", length=1)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ready = ws.receive_json()
        assert ready["state"] == "ready" and ready["of"] == 1
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        done, seen = recv_until(ws, "turn.done")
        deltas = [m["text"] for m in seen if m["type"] == "interviewer.delta"]
        assert deltas == ["Hi, I'm Sam.", "Tell me about a project you led."]  # tag stripped, sentence by sentence
        assert done["kind"] == "question" and done["question_no"] == 1 and not done["is_followup"]
        recv_until(ws, "status")

        ws.send_text(json.dumps({"type": "text", "text": "I led the billing rewrite, um, with three engineers."}))
        done, _ = recv_until(ws, "turn.done")
        assert done["is_followup"] and done["text"] == "What was the result?"
        recv_until(ws, "status")
        ws.send_text(json.dumps({"type": "text", "text": "Invoices went out twice as fast."}))
        done, _ = recv_until(ws, "turn.done")
        assert done["kind"] == "closing"
        ended, _ = recv_until(ws, "session.ended")
        wait_closed(ws)
        assert ended["reason"] == "length" and ended["feedback_pending"] is True

    assert "Do not ask another follow-up" not in s.calls[1]["prompt"]
    assert "The interview is over" not in s.calls[1]["prompt"]
    db = SessionLocal()
    try:
        row = db.get(VoiceSession, sid)
        assert row.state == "ended" and row.purge_at is not None and row.feedback_state == "pending"
        turns = voice_store.turns(db, sid)
        assert [(t.speaker, t.kind) for t in turns] == [("interviewer", "question"), ("user", "answer"),
                                                         ("interviewer", "question"), ("user", "answer"),
                                                         ("interviewer", "closing")]
        assert all("billing" not in t.text_sealed for t in turns)  # sealed at rest
        assert turns[1].metrics["filler_count"] == 1
    finally:
        db.close()
    assert any(c["task"] == "job_finder.voice_finalize" and c["args"] == [sid] for c in dispatched)

    detail = client.get(f"{API}/voice/sessions/{sid}", headers=headers).json()
    assert detail["turns"][1]["text"].startswith("I led the billing rewrite")
    assert detail["session"]["question_count"] == 2


def test_spoken_answer_goes_through_vad_and_stt(client, make_user, script, stt):
    _, headers = make_user()
    script(["[Q] Why this role?", "[END] Thanks."])
    stt.texts = ["Because I love building APIs."]
    created = start(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "turn.done")
        recv_until(ws, "status")
        ts = 0
        for ms, loud in ((100, False),) + ((100, True),) * 5 + ((100, False),) * 9:
            ws.send_bytes(tone(ms, ts, loud=loud))
            ts += ms
        final, _ = recv_until(ws, "transcript.final")
        assert final["text"] == "Because I love building APIs." and final["channel"] == "user"
        done, _ = recv_until(ws, "turn.done")
        assert done["kind"] == "closing" and "stt_ms" in done["latency"] and "first_word_ms" in done["latency"]
        wait_closed(ws)
    assert stt.seen_bytes > 0


def test_barge_in_marks_the_interviewer_turn_interrupted(client, make_user, script):
    _, headers = make_user()
    script(["[Q] Tell me about yourself. |And also something else that runs long."], block_after_first=True)
    created = start(client, headers)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "interviewer.delta")
        ws.send_text(json.dumps({"type": "control", "action": "barge_in"}))
        done, _ = recv_until(ws, "turn.done")
        assert done["interrupted"] is True and done["text"] == "Tell me about yourself."
        ws.send_text(json.dumps({"type": "control", "action": "end"}))
        recv_until(ws, "session.ended")
        wait_closed(ws)
    db = SessionLocal()
    try:
        assert [t.state for t in voice_store.turns(db, sid)] == ["interrupted"]
    finally:
        db.close()


def test_cost_cap_switches_to_free_models_then_ends(client, make_user, script):
    _, headers = make_user()
    s = script(["[Q] First question?", "[Q] Second?"], cost=1.0, fail_when_free_only=True)
    created = start(client, headers, length=3)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        _, seen = recv_until(ws, "status")
        while not any(m.get("cost_capped") for m in seen):
            seen.append(ws.receive_json())
        # Answer at once, before turn.done arrives: the server waits for the question to finish.
        ws.send_text(json.dumps({"type": "text", "text": "An answer."}))
        err, _ = recv_until(ws, "error")
        assert err["code"] == "session_cost_cap"
        ended, _ = recv_until(ws, "session.ended")
        wait_closed(ws)
        assert ended["reason"] == "cost_cap"
    assert s.calls[1]["free_only"] is True


def test_only_one_live_voice_session(client, make_user, script):
    _, headers = make_user()
    script([])
    first = start(client, headers)
    with client.websocket_connect(first["stream_path"]) as ws:
        auth(ws, first)
        ws.receive_json()
        resp = client.post(f"{API}/voice/sessions", json={}, headers=headers)
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "voice_session_busy"
        ws.send_text(json.dumps({"type": "control", "action": "end"}))
        recv_until(ws, "session.ended")


def test_oversized_frames_close_the_socket(client, make_user, script):
    _, headers = make_user()
    script([])
    created = start(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_bytes(tone(500, 0))
        assert ws.receive_json()["code"] == "frame_too_big"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == practice_session.CLOSE_TOO_BIG


def test_ended_session_refuses_new_sockets(client, make_user, script):
    _, headers = make_user()
    script([])
    created = start(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "end"}))
        recv_until(ws, "session.ended")
        wait_closed(ws)
    assert client.post(f"{API}/voice/sessions/{created['session']['id']}/token",
                       headers=headers).status_code == 409


def test_reconnect_resumes_the_transcript(client, make_user, script):
    _, headers = make_user()
    script(["[Q] Why us?"])
    created = start(client, headers, length=2)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "turn.done")
    token = client.post(f"{API}/voice/sessions/{sid}/token", headers=headers).json()["stream_token"]
    with client.websocket_connect(created["stream_path"]) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        resumed = ws.receive_json()
        assert resumed["type"] == "session.resumed" and resumed["turns"][0]["text"] == "Why us?"
        assert ws.receive_json()["state"] == "listening"


# ---------------------------------------------------------------- evaluation, history, retention

def _finished_session(client, headers, script):
    script(["[Q] Tell me about a project you led.", "[END] Thanks."])
    created = start(client, headers, length=1)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "turn.done")
        ws.send_text(json.dumps({"type": "text", "text": "At Acme I led the billing rewrite."}))
        recv_until(ws, "session.ended")
        wait_closed(ws)
    return created["session"]["id"]


def test_finalize_scores_answers_and_grounds_model_answers(client, make_user, script, monkeypatch):
    user_id, headers = make_user()
    sid = _finished_session(client, headers, script)
    reply = {"answers": [{"index": 1, "skill_area": "Leadership",
                          "scores": {"accuracy": 4, "relevance": 5, "structure": 2, "clarity": 3},
                          "star": {"situation": True, "action": True},
                          "strengths": ["Clear ownership."], "improvements": ["Add a measurable result."],
                          "model_answer": "At Acme I led the billing rewrite. It saved Google 40% in costs."}],
             "summary": "Solid start.", "strengths": ["Calm"], "improvements": ["Quantify results"]}

    class R:
        text = json.dumps(reply)
        cost_usd = 0.002
        engine = "fake:eval"

    monkeypatch.setattr(ai_router, "complete", lambda db, **kw: R())
    db = SessionLocal()
    try:
        assert voice_eval.finalize(db, sid)["status"] == "ready"
        assert voice_eval.finalize(db, sid)["status"] == "done"  # idempotent
    finally:
        db.close()

    fb = client.get(f"{API}/voice/sessions/{sid}/feedback", headers=headers).json()
    assert fb["state"] == "ready" and fb["overall_score"] == 3.5
    answer = fb["answers"][0]
    assert answer["question"] == "Tell me about a project you led."
    assert answer["model_answer"] == "At Acme I led the billing rewrite."  # the invented Google claim was dropped
    assert answer["skill_area"] == "leadership" and answer["star"]["result"] is False
    progress = client.get(f"{API}/voice/progress", headers=headers).json()
    assert progress["sessions"] == 1 and progress["areas"][0]["skill_area"] == "leadership"


def test_finalize_ends_a_dropped_session_after_the_resume_window(client, make_user, script):
    _, headers = make_user()
    script(["[Q] Why us?"])
    created = start(client, headers, length=2)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "turn.done")
    db = SessionLocal()
    try:
        assert voice_eval.finalize(db, sid)["status"] == "skip"  # still inside the resume window
        row = db.get(VoiceSession, sid)
        row.last_seen_at = row.last_seen_at.replace(year=2020)
        db.commit()
        assert voice_eval.finalize(db, sid)["status"] == "ready"  # no answers -> empty feedback
        db.expire_all()
        assert db.get(VoiceSession, sid).end_reason == "disconnected"
    finally:
        db.close()


def test_finalize_retries_while_every_model_is_busy_then_fails(client, make_user, script, monkeypatch, dispatched):
    _, headers = make_user()
    sid = _finished_session(client, headers, script)
    dispatched.clear()

    def busy(db, **kw):
        raise ai_router.NoModelAvailable("interview_eval", ["test: rate limited"])

    monkeypatch.setattr(ai_router, "complete", busy)
    db = SessionLocal()
    try:
        for attempt, delay in enumerate(voice_eval.RETRY_DELAYS):
            assert voice_eval.finalize(db, sid, attempt) == {"status": "retrying", "attempt": attempt + 1}
            db.expire_all()
            assert db.get(VoiceSession, sid).feedback_state == "pending"
            call = dispatched[-1]
            assert call["task"] == "job_finder.voice_finalize" and call["args"] == [sid]
            assert call["kwargs"] == {"attempt": attempt + 1} and call["options"]["countdown"] == delay
        assert voice_eval.finalize(db, sid, len(voice_eval.RETRY_DELAYS))["status"] == "failed"
        db.expire_all()
        assert db.get(VoiceSession, sid).feedback_state == "failed"
    finally:
        db.close()
    assert len(dispatched) == len(voice_eval.RETRY_DELAYS)


def test_failed_feedback_can_be_retried(client, make_user, script, dispatched):
    _, headers = make_user()
    sid = _finished_session(client, headers, script)
    url = f"{API}/voice/sessions/{sid}/feedback/retry"
    res = client.post(url, headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "feedback_not_failed"  # still pending

    db = SessionLocal()
    try:
        db.get(VoiceSession, sid).feedback_state = "failed"
        db.commit()
    finally:
        db.close()
    dispatched.clear()
    res = client.post(url, headers=headers)
    assert res.status_code == 202 and res.json()["state"] == "pending"
    assert dispatched == [{"task": "job_finder.voice_finalize", "args": [sid], "kwargs": None,
                           "options": {"queue": dispatched[0]["options"]["queue"]}}]

    _, other = make_user()
    assert client.post(url, headers=other).status_code == 404


def test_typed_answer_while_paused_is_refused_with_a_reason(client, make_user, script):
    _, headers = make_user()
    script(["[Q] Why this role?"])
    created = start(client, headers, length=2)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "turn.done")
        ws.send_text(json.dumps({"type": "control", "action": "pause"}))
        recv_until(ws, "status")
        ws.send_text(json.dumps({"type": "text", "text": "Because I like the product."}))
        err, seen = recv_until(ws, "error")
        assert err["code"] == "answer_not_expected" and err["retryable"] is True
        assert "Resume" in err["message"]
        assert not any(m["type"] == "transcript.final" for m in seen)


def test_history_delete_and_purge(client, make_user, script):
    user_id, headers = make_user()
    sid = _finished_session(client, headers, script)
    other = _finished_session(client, headers, script)
    hist = client.get(f"{API}/voice/history", headers=headers).json()
    assert hist["total"] == 2
    assert client.delete(f"{API}/voice/sessions/{sid}", headers=headers).status_code == 204
    db = SessionLocal()
    try:
        assert db.query(VoiceTurn).filter(VoiceTurn.session_id == sid).count() == 0
        row = db.get(VoiceSession, other)
        row.purge_at = row.purge_at.replace(year=2020)
        db.commit()
        assert voice_store.purge_expired(db) >= 1
        assert db.get(VoiceSession, other) is None
    finally:
        db.close()
    _finished_session(client, headers, script)
    assert client.delete(f"{API}/voice/history", headers=headers).json()["deleted"] == 1


def test_other_users_cannot_read_a_session(client, make_user, script):
    _, headers = make_user()
    _, other_headers = make_user()
    sid = _finished_session(client, headers, script)
    assert client.get(f"{API}/voice/sessions/{sid}", headers=other_headers).status_code == 404
    assert client.delete(f"{API}/voice/sessions/{sid}", headers=other_headers).status_code == 404


def test_no_audio_is_ever_stored(client, make_user, script, stt):
    _, headers = make_user()
    script(["[Q] Hello?", "[END] Bye."])
    stt.texts = ["Hi there."]
    created = start(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.receive_json()
        ws.send_text(json.dumps({"type": "control", "action": "start"}))
        recv_until(ws, "turn.done")
        ts = 0
        for ms, loud in ((100, True),) * 5 + ((100, False),) * 9:
            ws.send_bytes(tone(ms, ts, loud=loud))
            ts += ms
        recv_until(ws, "session.ended")
        wait_closed(ws)
    db = SessionLocal()
    try:
        for table in (VoiceSession, VoiceTurn, VoiceFeedback):
            for row in db.query(table).all():
                for col in table.__table__.columns:
                    value = getattr(row, col.name)
                    assert not isinstance(value, (bytes, bytearray)), (table, col.name)
                    assert len(str(value or "")) < 20000
    finally:
        db.close()


# ---------------------------------------------------------------- story bank

def test_story_bank_crud(client, make_user):
    _, headers = make_user()
    created = client.post(f"{API}/story-bank", json={"title": "Billing rewrite", "action": "Led it",
                                                     "skills": ["Python", "python", "Leadership"]},
                          headers=headers)
    assert created.status_code == 201 and created.json()["skills"] == ["Python", "Leadership"]
    sid = created.json()["id"]
    listed = client.get(f"{API}/story-bank", headers=headers).json()
    assert listed["total"] == 1 and listed["needs_review"] == 0
    updated = client.put(f"{API}/story-bank/{sid}", json={"title": "Billing", "result": "2x faster"},
                         headers=headers).json()
    assert updated["result"] == "2x faster"
    assert client.delete(f"{API}/story-bank/{sid}", headers=headers).status_code == 204


def test_story_drafts_are_grounded_and_wait_for_approval(make_user, monkeypatch):
    user_id, _ = make_user()
    from app.models.profile import Resume
    db = SessionLocal()
    try:
        db.add(Resume(user_id=user_id, version=1, is_master=True, file_name="cv.pdf", content_type="application/pdf",
                      size_bytes=1, sha256="x", text="Acme Corp — led the Billing rewrite in Python, cut costs 30%."))
        db.commit()

        class R:
            text = json.dumps({"stories": [
                {"title": "Billing rewrite", "situation": "Billing at Acme Corp was slow.",
                 "action": "I led the rewrite in Python. I also won a Microsoft award.",
                 "result": "Costs fell 30%.", "skills": ["Python", "Kubernetes"]}]})
        monkeypatch.setattr(ai_router, "complete", lambda db, **kw: R())
        assert story_bank.draft_from_resume(db, user_id)["created"] == 1
        story = db.query(StoryBankEntry).filter(StoryBankEntry.user_id == user_id).one()
        assert story.approved is False and story.origin == "ai"
        assert "Microsoft" not in story.action and story.skills == ["Python"]
    finally:
        db.close()
