"""
app/services/voice_health.py
------------------------------
GET /admin/voice/health (P7 plan 2.9, 2.11): how voice sessions are doing —
sessions by mode and end reason, latency p50/p95 per stage (from the
numbers stored on each turn), speech and AI model calls with failure rates
and spend from the usage ledger. No transcript text is read here.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.ai import UsageLedger
from app.models.voice import VoiceSession, VoiceTurn
from app.services import feature_flags

VOICE_TASKS = ("stt", "tts", "interview_chat", "interview_eval", "copilot_answer", "copilot_expand", "question_gate")
STAGES = {"mock": ("stt_ms", "first_token_ms", "first_word_ms", "reply_ms"),
          "copilot": ("gate_ms", "first_token_ms", "first_visible_ms", "answer_ms")}


def _pct(values: list[int]) -> dict:
    values = sorted(values)
    return {"p50": int(statistics.median(values)),
            "p95": values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))], "n": len(values)}


def summary(db: Session, *, since: datetime, days: int) -> dict:
    since_naive = since.replace(tzinfo=None)
    sessions = db.query(VoiceSession.id, VoiceSession.mode, VoiceSession.state, VoiceSession.end_reason).filter(
        VoiceSession.created_at >= since_naive).all()
    modes = {s.id: s.mode for s in sessions}
    by_mode: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        by_mode[s.mode]["total"] += 1
        by_mode[s.mode][f"state:{s.state}"] += 1
        if s.end_reason:
            by_mode[s.mode][f"ended:{s.end_reason}"] += 1

    samples: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    speculative = Counter()
    if modes:
        turns = db.query(VoiceTurn.session_id, VoiceTurn.latency).filter(
            VoiceTurn.session_id.in_(list(modes)), VoiceTurn.created_at >= since_naive).all()
        for t in turns:
            lat = t.latency or {}
            mode = modes.get(t.session_id)
            for key in STAGES.get(mode, ()):
                if isinstance(lat.get(key), (int, float)):
                    samples[mode][key].append(int(lat[key]))
            if mode == "copilot" and "speculative" in lat:
                speculative["yes" if lat["speculative"] else "no"] += 1
    latency = {mode: {k: _pct(v) for k, v in stages.items() if v} for mode, stages in samples.items()}
    if speculative:
        latency.setdefault("copilot", {})["speculative_share"] = round(
            speculative["yes"] / (speculative["yes"] + speculative["no"]), 2)

    rows = db.query(UsageLedger).filter(UsageLedger.task.in_(VOICE_TASKS), UsageLedger.created_at >= since_naive).all()
    groups: dict[tuple, list[UsageLedger]] = defaultdict(list)
    for r in rows:
        groups[(r.task, r.provider, r.model_id)].append(r)
    speech, llm = [], []
    for (task, provider, model_id), items in sorted(groups.items()):
        failures = [i for i in items if not i.success]
        entry = {"task": task, "provider": provider, "model_id": model_id, "calls": len(items),
                 "failures": len(failures), "error_rate": round(len(failures) / len(items), 3),
                 "latency_ms": _pct([i.latency_ms for i in items if i.success] or [0]),
                 "cost_usd": round(sum(i.cost_usd or 0 for i in items), 4),
                 "last_error": failures[-1].error if failures else None}
        if task in ("stt", "tts"):
            entry["units"] = sum(i.tokens_in or 0 for i in items)  # audio seconds / characters
            speech.append(entry)
        else:
            llm.append(entry)
    return {"days": days, "sessions": {m: dict(c) for m, c in by_mode.items()}, "latency": latency,
            "speech": speech, "llm": llm, "spend_usd": round(sum(r.cost_usd or 0 for r in rows), 4),
            "flags": {"live_copilot_enabled": bool(feature_flags.get_flag(db, "live_copilot_enabled"))}}
