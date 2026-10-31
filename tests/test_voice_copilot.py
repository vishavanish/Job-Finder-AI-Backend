"""
P7b — Live Copilot: question gate (labelled eval set as a CI gate),
acknowledgement and entitlement gates, Deepgram adapter, and the copilot
socket (cards, streaming answers, interrupts, speculative restart,
follow-ups, controls, screenshots, mixed channel, summary, admin health).
Fake speech and AI; no network.
"""
from __future__ import annotations

import array
import asyncio
import base64
import json
import math
import time
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.db import SessionLocal
from app.models.admin import AuditLog
from app.models.voice import VoiceSession, VoiceTurn
from app.providers.voice import deepgram
from app.providers.voice.base import Transcript
from app.providers.voice.vad import Segmenter
from app.services import ai_router, copilot_session, entitlements, feature_flags, voice_eval, voice_speech, voice_store
from app.services.question_gate import classify

API = "/api/v1"
FIXTURE = Path(__file__).parent / "fixtures" / "question_gate.jsonl"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()


# ================================================================ question gate

def _cases():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_question_gate_meets_the_eval_thresholds():
    """CI gate (CLAUDE.md 13): precision >= 0.95 and recall >= 0.9 on the labelled set (English + Hinglish)."""
    tp = fp = fn = 0
    misses = []
    for case in _cases():
        result = classify(case["text"], since_last_card_s=5 if case.get("recent") else None)
        if case["question"] and result.is_question:
            tp += 1
        elif case["question"]:
            fn += 1
            misses.append(case["text"])
        elif result.is_question:
            fp += 1
            misses.append(case["text"])
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    assert precision >= 0.95 and recall >= 0.9, (precision, recall, misses)


def test_question_gate_types_followups_and_multipart():
    typed = [c for c in _cases() if c["question"] and c.get("type") not in (None, "general")]
    right = sum(classify(c["text"]).qtype == c["type"] for c in typed)
    assert right / len(typed) >= 0.85
    for case in _cases():
        if case.get("followup"):
            assert classify(case["text"], since_last_card_s=5).is_followup, case["text"]
        if case.get("multipart"):
            assert classify(case["text"]).multipart, case["text"]
    assert not classify("What was the result?", since_last_card_s=120).is_followup  # too long after the card
    assert classify("Design a URL shortener.").hard
    assert classify("What is your notice period?").terminal
    assert not classify("Can you tell me about the").terminal
    assert classify("I really want to understand your thinking there").decision == "unsure"


# ================================================================ fakes

class FakeLLM:
    """Scripted copilot answers: each reply is (pieces, block_after_first)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def stream(self, db, *, user_id, task, system, user_prompt, outcome, max_tokens=800, ref=None,
               free_only=False, timeout=None, cancel=None, images=None, first_token_timeout=None):
        self.calls.append({"task": task, "prompt": user_prompt, "images": images, "system": system})
        pieces, block = self.replies.pop(0) if self.replies else (["- An answer."], False)
        outcome.first_token_ms = 3
        for i, piece in enumerate(pieces):
            yield piece
            if block and i == 0:
                while cancel is not None and not cancel.is_set():
                    time.sleep(0.01)
                return
        outcome.text = "".join(pieces)


class FakeStream:
    """A streaming STT: each audio frame sent releases the next scripted batch of transcripts."""

    def __init__(self, script):
        self.script = list(script)
        self.events: asyncio.Queue = asyncio.Queue()
        self.sent = 0

    async def start(self):
        pass

    async def send(self, pcm):
        self.sent += len(pcm)
        for t in (self.script.pop(0) if self.script else []):
            self.events.put_nowait(t)

    async def close(self):
        self.events.put_nowait(None)
        return self.sent / 32000


class FakeSTT:
    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts: list[str | None] = []

    async def transcribe(self, pcm, *, language=None, prompt=None):
        self.prompts.append(prompt)
        return Transcript(text=self.texts.pop(0) if self.texts else "")


@pytest.fixture
def llm(monkeypatch):
    def install(replies):
        fake = FakeLLM(replies)
        monkeypatch.setattr(ai_router, "stream", fake.stream)
        return fake
    return install


@pytest.fixture
def speech(monkeypatch):
    """Per-utterance fake STT by default; .stream(script) switches to a fake streaming STT."""
    state = {"stt": FakeSTT([]), "stream": None}
    whisper = voice_speech.SpeechChoice("fake-stt", "groq", "whisper-fake", "Fake Whisper", "free", None, "", "none")
    deep = voice_speech.SpeechChoice("fake-dg", "deepgram", "nova-3", "Fake Deepgram", "paid", None, "k", "byok")

    def resolve(db, *, user_id, task, free_only=False):
        if task == "stt":
            return ([deep, whisper] if state["stream"] is not None else [whisper]), []
        return [], []

    monkeypatch.setattr(voice_speech, "resolve", resolve)
    monkeypatch.setattr(voice_speech, "make_stt", lambda c: state["stt"])
    monkeypatch.setattr(voice_speech, "make_streaming_stt", lambda c, **kw: state["stream"])
    monkeypatch.setattr(voice_speech, "_ledger", lambda *a, **k: 0.0)
    monkeypatch.setattr(copilot_session, "Segmenter", lambda **kw: Segmenter(**{**kw, "use_webrtc": False}))

    class Handle:
        def texts(self, texts):
            state["stt"].texts = list(texts)
            return state["stt"]

        def stream(self, script):
            state["stream"] = FakeStream(script)
            return state["stream"]
    return Handle()


def frame(channel: int, ms: int, ts: int, loud=True) -> bytes:
    samples = array.array("h", (int(8000 * math.sin(i / 5)) if loud else 0 for i in range(ms * 16)))
    return bytes([channel]) + ts.to_bytes(4, "little") + samples.tobytes()


def speak(ws, channel, ts=0, *, talk=5, silence=8):
    for i in range(talk + silence):
        ws.send_bytes(frame(channel, 100, ts + i * 100, loud=i < talk))
    return ts + (talk + silence) * 100


def recv_until(ws, kind, limit=80, **match):
    seen = []
    for _ in range(limit):
        try:
            msg = ws.receive_json()
        except WebSocketDisconnect as e:
            raise AssertionError(f"closed {e.code} waiting for {kind}; got {[m.get('type') for m in seen]}")
        seen.append(msg)
        if msg["type"] == kind and all(msg.get(k) == v for k, v in match.items()):
            return msg, seen
    raise AssertionError(f"no {kind} in {[m['type'] for m in seen]}")


def wait_closed(ws):
    for _ in range(40):
        try:
            ws.receive_json()
        except WebSocketDisconnect:
            return
    raise AssertionError("socket did not close")


def stop(ws):
    ws.send_text(json.dumps({"type": "control", "action": "stop"}))
    _, seen = recv_until(ws, "session.ended")
    wait_closed(ws)
    return seen


def enable_copilot(user_id: str, *, on=True):
    db = SessionLocal()
    try:
        entitlements.set_override(db, user_id, "live_copilot", enabled=on, quota_per_day=None, max_level=None,
                                  expires_at=None)
        db.commit()
    finally:
        db.close()


def set_kill_switch(user_id: str, value: bool):
    db = SessionLocal()
    try:
        feature_flags.set_flag(db, "live_copilot_enabled", value, actor_id=user_id)
        db.commit()
    finally:
        db.close()


def ready_user(client, make_user):
    user_id, headers = make_user()
    enable_copilot(user_id)
    version = client.get(f"{API}/voice/settings", headers=headers).json()["copilot_ack_version"]
    assert client.post(f"{API}/voice/copilot/acknowledge", json={"version": version},
                       headers=headers).status_code == 200
    return user_id, headers


def open_copilot(client, headers, **body):
    resp = client.post(f"{API}/voice/sessions", json={"mode": "copilot", **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth(ws, created):
    ws.send_text(json.dumps({"type": "auth", "token": created["stream_token"]}))
    status = ws.receive_json()
    assert status["type"] == "status" and status["state"] == "listening", status
    return status


# ================================================================ gates

def test_copilot_needs_entitlement_kill_switch_and_acknowledgement(client, make_user):
    user_id, headers = make_user()
    body = {"mode": "copilot"}
    assert client.post(f"{API}/voice/sessions", json=body, headers=headers).status_code == 403  # off by default
    enable_copilot(user_id)
    resp = client.post(f"{API}/voice/sessions", json=body, headers=headers)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "copilot_ack_required"
    assert client.post(f"{API}/voice/copilot/acknowledge", json={"version": "1999-01"},
                       headers=headers).status_code == 409
    version = client.get(f"{API}/voice/settings", headers=headers).json()["copilot_ack_version"]
    acked = client.post(f"{API}/voice/copilot/acknowledge", json={"version": version}, headers=headers).json()
    assert acked["copilot_acknowledged"] is True
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(AuditLog.actor_id == user_id,
                                         AuditLog.action == "voice.copilot_acknowledge").count() == 1
    finally:
        db.close()
    set_kill_switch(user_id, False)
    assert client.post(f"{API}/voice/sessions", json=body, headers=headers).status_code == 503
    set_kill_switch(user_id, True)
    created = client.post(f"{API}/voice/sessions", json=body, headers=headers).json()
    assert created["stream_path"].endswith(f"/voice/copilot/{created['session']['id']}/stream")
    assert created["session"]["config"]["sources"] == "tab_mic"


def test_a_new_acknowledgement_version_is_asked_again(client, make_user, monkeypatch):
    _, headers = ready_user(client, make_user)
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "COPILOT_ACK_VERSION", "2027-01")
    resp = client.post(f"{API}/voice/sessions", json={"mode": "copilot"}, headers=headers)
    assert resp.json()["detail"]["code"] == "copilot_ack_required"


def test_socket_rechecks_the_entitlement(client, make_user, speech):
    user_id, headers = ready_user(client, make_user)
    created = open_copilot(client, headers)
    enable_copilot(user_id, on=False)
    with client.websocket_connect(created["stream_path"]) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": created["stream_token"]}))
        assert ws.receive_json()["code"] == "feature_disabled"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4403


def test_a_practice_token_cannot_open_the_copilot_socket(client, make_user, speech):
    _, headers = ready_user(client, make_user)
    practice = client.post(f"{API}/voice/sessions", json={"mode": "mock"}, headers=headers).json()
    with client.websocket_connect(f"/api/v1/voice/copilot/{practice['session']['id']}/stream") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": practice["stream_token"]}))
        assert ws.receive_json()["code"] == "auth_failed"


def test_settings_offer_no_stealth_options():
    from app.models.voice_schemas import SessionCreateIn, VoiceSettingsIn
    fields = set(VoiceSettingsIn.model_fields) | set(SessionCreateIn.model_fields)
    for word in ("hide", "stealth", "invisible", "screen_share", "undetect", "evasion", "proctor"):
        assert not any(word in f for f in fields), word


# ================================================================ the loop

def test_interviewer_questions_become_cards_and_small_talk_is_ignored(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["- Led the billing rewrite.\n", "- Shipped in 6 weeks."], False)])
    speech.texts(["Can you hear me okay?", "Tell me about a time you led a project.", "I led the billing rewrite."])
    created = open_copilot(client, headers)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        assert auth(ws, created)["streaming"] is False
        ts = speak(ws, 0)
        final, _ = recv_until(ws, "transcript.final")
        assert final["text"] == "Can you hear me okay?" and final["channel"] == "interviewer"
        ts = speak(ws, 0, ts)
        detected, seen = recv_until(ws, "question.detected")
        assert not any(m["type"] == "question.detected" for m in seen[:-1])
        assert detected["text"] == "Tell me about a time you led a project." and detected["qtype"] == "behavioral"
        done, seen = recv_until(ws, "answer.done")
        deltas = "".join(m["text"] for m in seen if m["type"] == "answer.delta")
        assert deltas == done["text"] == "- Led the billing rewrite.\n- Shipped in 6 weeks."
        assert done["question_id"] == detected["question_id"] and "first_visible_ms" in done["latency"]
        speak(ws, 1, ts)  # the candidate's own speech: context only
        user_final, _ = recv_until(ws, "transcript.final", channel="user")
        assert user_final["text"] == "I led the billing rewrite."
        stop(ws)
    assert fake.calls[0]["task"] == "copilot_answer" and "never invent" in fake.calls[0]["system"]
    db = SessionLocal()
    try:
        kinds = {(t.speaker, t.kind) for t in voice_store.turns(db, sid)}
        assert {("interviewer", "utterance"), ("interviewer", "question"), ("assistant", "suggestion"),
                ("user", "utterance")} <= kinds
        assert db.get(VoiceSession, sid).state == "ended"
    finally:
        db.close()


def test_new_question_interrupts_the_streaming_answer(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    llm([(["- First part. ", "never finished"], True), (["- Second answer."], False)])
    speech.texts(["What is your notice period?", "Why do you want to join us?"])
    created = open_copilot(client, headers)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ts = speak(ws, 0)
        first, _ = recv_until(ws, "question.detected")
        recv_until(ws, "answer.delta")
        speak(ws, 0, ts)
        cancelled, _ = recv_until(ws, "answer.cancelled")
        assert cancelled == {"type": "answer.cancelled", "question_id": first["question_id"], "reason": "interrupted"}
        second, _ = recv_until(ws, "question.detected")
        done, _ = recv_until(ws, "answer.done")
        assert done["question_id"] == second["question_id"] != first["question_id"]
        stop(ws)
    db = SessionLocal()
    try:
        states = {t.question_id: t.state for t in voice_store.turns(db, sid) if t.kind == "suggestion"}
        assert states[first["question_id"]] == "interrupted" and states[second["question_id"]] == "shown"
    finally:
        db.close()


def test_follow_ups_thread_under_their_question(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["- Kafka for events."], False), (["- Because ordering mattered."], False)])
    speech.texts(["Which message queue did you use?", "Why?"])
    created = open_copilot(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ts = speak(ws, 0)
        first, _ = recv_until(ws, "question.detected")
        recv_until(ws, "answer.done")
        speak(ws, 0, ts)
        follow, _ = recv_until(ws, "question.detected")
        assert follow["is_followup"] is True and follow["parent_id"] == first["question_id"]
        recv_until(ws, "answer.done")
        stop(ws)
    assert "Earlier question: Which message queue did you use?" in fake.calls[1]["prompt"]


def test_speculative_start_restarts_when_the_speaker_keeps_talking(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["- Thirty days. ", "(cut)"], True), (["- Thirty days, and 18 LPA expected."], False)])
    stream = speech.stream([
        [Transcript("What is your notice period?", final=False)],
        [Transcript("What is your notice period and your expected CTC?", final=False)],
        [Transcript("What is your notice period and your expected CTC?", final=True, utterance_end=True)],
    ])
    created = open_copilot(client, headers)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        status = auth(ws, created)
        assert status["streaming"] is True and status["speculative"] is True
        ws.send_bytes(frame(0, 100, 0))
        detected, _ = recv_until(ws, "question.detected")
        assert detected["speculative"] is True and detected["text"] == "What is your notice period?"
        recv_until(ws, "answer.delta")
        ws.send_bytes(frame(0, 100, 100))
        cancelled, _ = recv_until(ws, "answer.cancelled")
        assert cancelled["reason"] == "restarted"
        updated, _ = recv_until(ws, "question.updated")
        assert updated["question_id"] == detected["question_id"] and "expected CTC" in updated["text"]
        done, _ = recv_until(ws, "answer.done")
        assert done["question_id"] == detected["question_id"] and done["latency"]["speculative"] is True
        ws.send_bytes(frame(0, 100, 200))  # the final: same question, no new card
        seen = stop(ws)
        assert not any(m["type"] == "question.detected" for m in seen)
    assert "expected CTC" in fake.calls[1]["prompt"] and stream.sent > 0
    db = SessionLocal()
    try:
        questions = [t for t in voice_store.turns(db, sid) if t.kind == "question"]
        assert len(questions) == 1 and "expected CTC" in voice_store.unseal(questions[0].text_sealed)
    finally:
        db.close()


def test_controls_expand_dismiss_and_answer_now(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["- Short answer."], False), (["- A much longer answer."], False), (["- Forced answer."], False)])
    speech.texts(["How did you scale the billing service?", "We use Kafka heavily on this team."])
    created = open_copilot(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ts = speak(ws, 0)
        card, _ = recv_until(ws, "question.detected")
        recv_until(ws, "answer.done")
        ws.send_text(json.dumps({"type": "control", "action": "expand", "question_id": card["question_id"]}))
        assert recv_until(ws, "answer.cancelled")[0]["reason"] == "regenerating"
        done, _ = recv_until(ws, "answer.done")
        assert done["text"] == "- A much longer answer." and done["latency"]["mode"] == "expand"
        ws.send_text(json.dumps({"type": "control", "action": "dismiss", "question_id": card["question_id"]}))
        assert recv_until(ws, "answer.cancelled")[0]["reason"] == "dismissed"
        speak(ws, 0, ts)  # a statement: no card
        recv_until(ws, "transcript.final")
        ws.send_text(json.dumps({"type": "control", "action": "answer_now"}))
        forced, _ = recv_until(ws, "question.detected")
        assert forced["forced"] is True and "Kafka" in forced["text"]
        recv_until(ws, "answer.done")
        ws.send_text(json.dumps({"type": "control", "action": "expand", "question_id": "nope"}))
        assert recv_until(ws, "error")[0]["code"] == "unknown_question"
        stop(ws)
    assert [c["task"] for c in fake.calls] == ["copilot_answer", "copilot_expand", "copilot_answer"]


def test_hard_questions_start_on_the_fast_model_and_cards_show_the_timing(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["- Hash the URL."], False)])
    speech.texts(["How would you design a URL shortener?"])
    created = open_copilot(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        speak(ws, 0)
        recv_until(ws, "question.detected")
        done, _ = recv_until(ws, "answer.done")
        stop(ws)
    assert fake.calls[0]["task"] == "copilot_answer"
    assert "requirements, main components" in fake.calls[0]["system"]
    lat = done["latency"]
    assert lat["end_wait_ms"] == 450 and lat["stt_ms"] >= 0
    assert lat["total_ms"] >= lat["end_wait_ms"] + lat["stt_ms"]


def test_screenshot_goes_to_one_vision_call_and_is_never_stored(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["Approach: two pointers."], False)])
    created = open_copilot(client, headers)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.send_text(json.dumps({"type": "screenshot", "image": "data:text/html;base64,PGI+"}))
        assert recv_until(ws, "error")[0]["code"] == "bad_screenshot"
        ws.send_text(json.dumps({"type": "screenshot", "image": PNG}))
        card, _ = recv_until(ws, "question.detected")
        assert card["qtype"] == "coding"
        recv_until(ws, "answer.done")
        stop(ws)
    mime, data = fake.calls[0]["images"][0]
    assert mime == "image/png" and data.startswith(b"\x89PNG") and fake.calls[0]["task"] == "copilot_expand"
    db = SessionLocal()
    try:
        for t in db.query(VoiceTurn).filter(VoiceTurn.session_id == sid).all():
            text = voice_store.unseal(t.text_sealed)
            assert "base64" not in text and "PNG" not in text and len(t.text_sealed) < 2000
    finally:
        db.close()


def test_mixed_channel_learns_which_speaker_is_the_user(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    llm([(["- Answer."], False)])
    speech.stream([
        [Transcript("Tell me about your last project.", final=True, utterance_end=True, speaker=0)],
        [Transcript("Can you explain the event loop?", final=True, utterance_end=True, speaker=0)],
    ])
    created = open_copilot(client, headers, sources="mic")
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.send_bytes(frame(2, 100, 0))
        card, _ = recv_until(ws, "question.detected")
        recv_until(ws, "answer.done")
        ws.send_text(json.dumps({"type": "control", "action": "that_was_me", "question_id": card["question_id"]}))
        recv_until(ws, "answer.cancelled", reason="dismissed")
        ws.send_bytes(frame(2, 100, 100))
        mine, _ = recv_until(ws, "transcript.final")
        assert mine["channel"] == "user"
        seen = stop(ws)
        assert not any(m["type"] == "question.detected" for m in seen)


def test_pause_ignores_audio(client, make_user, speech, llm):
    _, headers = ready_user(client, make_user)
    llm([])
    speech.texts(["What is your notice period?"])
    created = open_copilot(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        ws.send_text(json.dumps({"type": "control", "action": "pause"}))
        assert recv_until(ws, "status")[0]["state"] == "paused"
        speak(ws, 0)
        seen = stop(ws)
        assert not any(m["type"] in ("transcript.final", "question.detected") for m in seen)


# ================================================================ after the session

def test_copilot_summary_and_admin_health(client, make_user, make_admin, speech, llm, monkeypatch):
    _, headers = ready_user(client, make_user)
    llm([(["- Thirty days."], False)])
    speech.texts(["What is your notice period?"])
    created = open_copilot(client, headers)
    sid = created["session"]["id"]
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        speak(ws, 0)
        recv_until(ws, "answer.done")
        stop(ws)

    class R:
        text = json.dumps({"summary": "HR round.", "weak_topics": ["compensation"],
                           "prepare": ["What are your salary expectations?"], "improvements": ["Know your numbers"]})
        cost_usd = 0.001
        engine = "fake:eval"

    monkeypatch.setattr(ai_router, "complete", lambda db, **kw: R())
    db = SessionLocal()
    try:
        assert voice_eval.finalize(db, sid)["status"] == "ready"
    finally:
        db.close()
    fb = client.get(f"{API}/voice/sessions/{sid}/feedback", headers=headers).json()
    assert fb["summary"] == "HR round." and fb["weak_questions"] == ["What are your salary expectations?"]
    assert fb["answers"][0]["question"] == "What is your notice period?"
    assert fb["answers"][0]["answer"] == "- Thirty days."

    _, admin = make_admin()
    health = client.get(f"{API}/admin/voice/health", headers=admin).json()
    assert health["sessions"]["copilot"]["total"] >= 1
    assert "first_visible_ms" in health["latency"]["copilot"]
    assert client.get(f"{API}/admin/voice/health", headers=headers).status_code == 403


# ================================================================ Deepgram adapter

def test_deepgram_messages_parse_into_transcripts():
    partial = deepgram.parse_message(json.dumps({
        "type": "Results", "is_final": False, "speech_final": False, "start": 1.0, "duration": 0.5,
        "channel": {"alternatives": [{"transcript": "What is your", "words": [
            {"word": "what", "punctuated_word": "What", "start": 1.0, "end": 1.1, "speaker": 1}]}]}}))
    assert partial.text == "What is your" and not partial.final and partial.speaker == 1
    final = deepgram.parse_message(json.dumps({
        "type": "Results", "is_final": True, "speech_final": True, "start": 1.0, "duration": 1.2,
        "channel": {"alternatives": [{"transcript": "What is your notice period?", "words": []}]}}))
    assert final.final and final.utterance_end and final.end_ms == 2200
    assert deepgram.parse_message(json.dumps({"type": "UtteranceEnd", "last_word_end": 2.5})).utterance_end
    assert deepgram.parse_message(json.dumps({"type": "Metadata"})) is None
    url = deepgram.listen_url(model_id="nova-3", language="en", diarize=True, endpointing_ms=300,
                              utterance_end_ms=1000)
    assert "diarize=true" in url and "interim_results=true" in url and "sample_rate=16000" in url


def test_deepgram_stream_sends_audio_and_reads_results():
    class FakeWS:
        def __init__(self):
            self.sent = []
            self.inbox: asyncio.Queue = asyncio.Queue()

        async def send(self, data):
            self.sent.append(data)
            if isinstance(data, bytes):
                await self.inbox.put(json.dumps({
                    "type": "Results", "is_final": True, "speech_final": True, "start": 0, "duration": 0.1,
                    "channel": {"alternatives": [{"transcript": "Hello there.", "words": []}]}}))
            elif json.loads(data).get("type") == "CloseStream":
                await self.inbox.put(None)

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.inbox.get()
            if item is None:
                raise StopAsyncIteration
            return item

    seen = {}

    async def connect(url, **kw):
        seen["headers"] = kw["additional_headers"]
        seen["ws"] = FakeWS()
        return seen["ws"]

    async def run():
        stt = deepgram.DeepgramStreamingSTT(model_id="nova-3", api_key="secret", connect=connect)
        await stt.start()
        await stt.send(b"\0" * 3200)
        t = await asyncio.wait_for(stt.events.get(), 2)
        seconds = await stt.close()
        return t, seconds

    t, seconds = asyncio.run(run())
    assert t.text == "Hello there." and t.utterance_end
    assert seconds == pytest.approx(0.1) and seen["headers"]["Authorization"] == "Token secret"
    assert any(isinstance(m, str) and "CloseStream" in m for m in seen["ws"].sent)


def test_coding_tasks_said_as_statements_get_code_and_whisper_gets_an_interview_hint(client, make_user, speech,
                                                                                    llm):
    _, headers = ready_user(client, make_user)
    fake = llm([(["Use a set.\n```python\nseen = set()\n```\nO(n) time, O(n) space."], False)])
    stt = speech.texts(["Right there we have to find duplicate element in the list."])
    created = open_copilot(client, headers)
    with client.websocket_connect(created["stream_path"]) as ws:
        auth(ws, created)
        speak(ws, 0)
        detected, _ = recv_until(ws, "question.detected")
        done, _ = recv_until(ws, "answer.done")
        stop(ws)
    assert detected["qtype"] == "coding"
    system = fake.calls[0]["system"]
    assert "code block" in system and "technical question" in system and "start with" not in system
    assert done["not_sure"] is False  # no resume match needed for code
    assert stt.prompts and "Write a program" in stt.prompts[0]

