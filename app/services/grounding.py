"""
app/services/grounding.py
----------------------------
Deterministic "did the AI invent this?" check (CLAUDE.md principle 3,
P4 plan §3.4–3.5). Used on tailored resume text, cover letters and AI
form answers.

A claim is a number or a name-like term: a word with an inner capital
(FastAPI, AWS), a digit or a symbol (S3, C++, node.js, GPT-4), or a
capitalised word that does not start a sentence (Kubernetes, Google).
A claim is grounded when the same term appears in the allowed text — the
user's resume, plus the job post where mentioning it is fair (a cover
letter may name the company and the role).

It is deliberately strict: a sentence with one ungrounded claim is
dropped, not repaired. Missing something true costs less than stating
something false on a job application.
"""
from __future__ import annotations

import re

from app.services.search_profiles import contains

_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-/]*[A-Za-z0-9+#]|[A-Za-z]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
# Capitalised words that are not claims.
_PLAIN = {
    "i", "a", "an", "the", "my", "me", "we", "our", "you", "your", "this", "that", "these", "those", "it", "its",
    "in", "on", "at", "for", "with", "and", "or", "but", "as", "by", "of", "to", "from", "into", "across",
    "dear", "hiring", "team", "manager", "regards", "best", "sincerely", "thank", "thanks", "hello", "hi",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
    "dec", "monday", "tuesday", "wednesday", "thursday", "friday", "present", "resume", "cv", "role",
    "position", "company", "led", "built", "designed", "developed", "delivered", "improved", "reduced",
    "increased", "managed", "created", "implemented", "launched", "owned", "drove", "worked",
}


def _norm_number(n: str) -> str:
    """By value, so "5", "5.0" and "1,000" / "1000" match."""
    n = n.replace(",", "")
    try:
        return f"{float(n):g}"
    except ValueError:
        return n


def claims(text: str) -> list[str]:
    """Numbers and name-like terms in `text`, in order, without repeats."""
    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        if term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)

    for n in _NUMBER.findall(text or ""):
        add(_norm_number(n))
    for sentence in _SENTENCE.split(text or ""):
        for i, m in enumerate(_WORD.finditer(sentence)):
            word = m.group(0).rstrip(".-/")
            if not word or word.lower() in _PLAIN:
                continue
            inner_cap = any(c.isupper() for c in word[1:])
            symbol = any(c.isdigit() or c in "+#./" for c in word)
            mid_capital = i > 0 and word[0].isupper()
            if inner_cap or symbol or mid_capital:
                add(word)
    return out


class Allowed:
    """The text the AI may draw facts from, prepared once for many checks."""

    def __init__(self, *texts: str):
        self.text = "\n".join(t for t in texts if t).lower()
        self.numbers = {_norm_number(n) for n in _NUMBER.findall(self.text)}

    def grounded(self, claim: str) -> bool:
        if claim[:1].isdigit():
            return _norm_number(claim) in self.numbers
        return contains(self.text, claim.lower())

    def ungrounded(self, text: str) -> list[str]:
        return [c for c in claims(text) if not self.grounded(c)]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


def keep_grounded_sentences(text: str, allowed: Allowed) -> tuple[str, list[str]]:
    """Paragraphs kept, minus sentences with ungrounded claims. Returns
    (clean text, dropped sentences)."""
    dropped: list[str] = []
    paragraphs = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        kept = []
        for sentence in split_sentences(para):
            if allowed.ungrounded(sentence):
                dropped.append(sentence)
            else:
                kept.append(sentence)
        if kept:
            paragraphs.append(" ".join(kept))
    return "\n\n".join(paragraphs), dropped
