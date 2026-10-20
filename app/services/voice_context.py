"""
app/services/voice_context.py
-------------------------------
What a voice session knows about the user and the job (P7 plan 2.4–2.5),
plus small text helpers shared by Practice and Live Copilot:

  - load(): job description, resume text and approved story-bank entries,
    cut into chunks for retrieval;
  - retrieve(): keyword ranking (no embeddings in P7) — the top chunks for a
    question and a 0–1 grounding score;
  - delivery(): words per minute, filler words and long pauses from the
    transcript and utterance times (numbers only, never audio);
  - sentences(): splits streamed text into speakable sentences.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.jobs import Job
from app.models.profile import Profile, Resume
from app.models.voice import StoryBankEntry, VoiceSession

JOB_CHARS = 3000
RESUME_CHARS = 4000
CHUNK_CHARS = 450

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_STOP = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "by", "from", "as", "is",
    "are", "was", "were", "be", "been", "it", "this", "that", "these", "those", "you", "your", "i", "me", "my", "we",
    "our", "they", "their", "he", "she", "what", "how", "why", "when", "where", "which", "who", "do", "did", "does",
    "can", "could", "would", "should", "will", "about", "tell", "describe", "explain", "walk", "through", "time",
    "have", "has", "had", "not", "so", "if", "then", "there", "any", "some", "one", "give", "example",
}
FILLERS = ("um", "uh", "erm", "hmm", "like", "you know", "basically", "actually", "literally", "sort of",
           "kind of", "i mean", "right")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class Chunk:
    source: str  # resume | job | story
    text: str
    words: set[str] = field(default_factory=set)


@dataclass
class SessionContext:
    job_title: str = ""
    company: str = ""
    job_text: str = ""
    role: str = ""
    resume_text: str = ""
    headline: str = ""
    stories: list[StoryBankEntry] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def target(self) -> str:
        if self.job_title:
            return f"{self.job_title} at {self.company}" if self.company else self.job_title
        return self.role or "the role the candidate is preparing for"

    def allowed_text(self) -> str:
        """What a model answer may draw facts from (grounding)."""
        return "\n".join([self.resume_text, self.headline, *(story_text(s) for s in self.stories)])


def words(text: str) -> set[str]:
    return {w.rstrip(".") for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def story_text(s: StoryBankEntry) -> str:
    parts = [s.title, f"Situation: {s.situation}" if s.situation else "", f"Task: {s.task}" if s.task else "",
             f"Action: {s.action}" if s.action else "", f"Result: {s.result}" if s.result else "",
             f"Skills: {', '.join(s.skills or [])}" if s.skills else ""]
    return "\n".join(p for p in parts if p)


def _chunks(source: str, text: str) -> list[Chunk]:
    out: list[Chunk] = []
    buf = ""
    for para in re.split(r"\n\s*\n|\n(?=[-•*])", text or ""):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) > CHUNK_CHARS and buf:
            out.append(Chunk(source, buf, words(buf)))
            buf = ""
        buf = f"{buf}\n{para}".strip()
    if buf:
        out.append(Chunk(source, buf, words(buf)))
    return out


def load(db: Session, session: VoiceSession) -> SessionContext:
    ctx = SessionContext(role=session.role or "")
    if session.job_id:
        job = db.get(Job, session.job_id)
        if job is not None:
            ctx.job_title, ctx.company = job.title or "", job.company or ""
            ctx.job_text = (job.description or "")[:JOB_CHARS]
    resume = db.get(Resume, session.resume_id) if session.resume_id else None
    if resume is not None and resume.user_id == session.user_id:
        ctx.resume_text = (resume.text or "")[:RESUME_CHARS]
    profile = db.query(Profile).filter(Profile.user_id == session.user_id).first()
    if profile is not None:
        ctx.headline = profile.headline or ""
    ctx.stories = (db.query(StoryBankEntry)
                   .filter(StoryBankEntry.user_id == session.user_id, StoryBankEntry.approved.is_(True))
                   .order_by(StoryBankEntry.updated_at.desc()).limit(50).all())
    for s in ctx.stories:
        db.expunge(s)
    ctx.chunks = (_chunks("resume", ctx.resume_text) + _chunks("job", ctx.job_text)
                  + [Chunk("story", story_text(s), words(story_text(s))) for s in ctx.stories])
    return ctx


def retrieve(ctx: SessionContext, query: str, k: int = 3) -> tuple[list[Chunk], float]:
    """Top-k chunks by keyword overlap, and a 0–1 score for the best one
    (share of the question's content words it covers)."""
    q = words(query)
    if not q or not ctx.chunks:
        return [], 0.0
    scored = []
    for c in ctx.chunks:
        overlap = len(q & c.words)
        if overlap:
            weight = 1.15 if c.source == "story" else 1.0
            scored.append((overlap * weight / math.sqrt(max(len(c.words), 1)), overlap, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [c for _, _, c in scored[:k]]
    best = max((o for _, o, _ in scored[:k]), default=0)
    return top, min(1.0, best / len(q))


def delivery(text: str, utterances: list[tuple[int, int]]) -> dict:
    """Pace and filler words for one answer. utterances = [(start_ms, end_ms)]."""
    lowered = f" {(text or '').lower()} "
    word_count = len((text or "").split())
    spoken_ms = sum(max(0, e - s) for s, e in utterances)
    fillers: dict[str, int] = {}
    for f in FILLERS:
        n = len(re.findall(rf"(?<![a-z']){re.escape(f)}(?![a-z'])", lowered))
        if n and not (f in ("like", "right") and n < 2):
            fillers[f] = n
    pauses = sum(1 for (_, e1), (s2, _) in zip(utterances, utterances[1:]) if s2 - e1 >= 2000)
    return {
        "words": word_count,
        "spoken_seconds": round(spoken_ms / 1000, 1),
        "wpm": round(word_count / (spoken_ms / 60000)) if spoken_ms >= 3000 else None,
        "fillers": fillers,
        "filler_count": sum(fillers.values()),
        "long_pauses": pauses,
    }


def split_ready(buffer: str) -> tuple[list[str], str]:
    """Complete sentences in `buffer` and the unfinished rest."""
    parts = _SENTENCE_END.split(buffer)
    if len(parts) == 1:
        return [], buffer
    done = [p.strip() for p in parts[:-1] if p.strip()]
    return done, parts[-1]
