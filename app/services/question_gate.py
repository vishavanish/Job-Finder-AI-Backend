"""
app/services/question_gate.py
-------------------------------
Live Copilot question gate (CLAUDE.md 7.10 B, P7 plan 2.5): decides, in
well under 50 ms, whether what the interviewer just said is a question the
candidate should answer, and what kind.

Rules first. Only the small "unsure" band (no question cue, but it sounds
addressed to the candidate) may ask the tiny `question_gate` model; that
call is time-boxed by the caller and rejects by default.

classify() also says:
  - is_followup: a short probe ("why?", "what about scale?") within
    FOLLOWUP_WINDOW_S of the previous card;
  - multipart: two or more asks in one turn (answered as numbered points);
  - hard: coding / system design / long questions (strong model first);
  - terminal: the text looks finished, so a PARTIAL transcript may start a
    speculative answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FOLLOWUP_WINDOW_S = 30.0

_LEAD = re.compile(r"^(?:(?:so|okay|ok|alright|all right|right|um+|uh+|hmm+|and|well|great|cool|nice|good|"
                    r"perfect|got it|thanks|thank you|sure|yeah|yes|now|next|also|then)[\s,.!]+)+")
_SPACE = re.compile(r"\s+")

SMALL_TALK = (
    "can you hear me", "can you hear us", "hear me okay", "hear me ok", "hear me now", "am i audible",
    "is my audio", "is my screen", "can you see my screen", "can you see me", "can you see the screen",
    "you're on mute", "you are on mute", "are you on mute", "let me share", "one second", "one moment",
    "give me a moment", "give me a second", "just a second", "how are you", "how's it going", "how is it going",
    "how are you doing", "how was your day", "how's your day", "nice to meet you", "good to meet you",
    "thanks for joining", "thank you for joining", "thanks for your time", "thank you for your time",
    "is the connection", "did i lose you", "are you still there", "can you repeat that please",
    "shall we get started", "shall we begin", "should we get started", "let's get started", "let's begin",
    "did you find the place", "is this a good time", "do you need a minute",
)
WH = ("what", "why", "how", "when", "where", "which", "who", "whom", "whose")
AUX = ("can you", "could you", "would you", "will you", "do you", "did you", "have you", "are you", "were you",
       "is there", "are there", "was there", "is it", "should you", "shall we", "have you ever", "do you have",
       "would it", "could it", "is that", "was that", "did that")
IMPERATIVE = (
    "tell me", "tell us", "walk me through", "walk us through", "take me through", "talk me through",
    "talk about", "describe", "explain", "give me an example", "give an example", "give me a", "share an example",
    "share a time", "share an instance", "share with me", "imagine", "suppose", "let's say", "lets say",
    "say you", "design a", "design an", "design the", "implement", "write a", "write code", "write me",
    "i'd like to hear", "i would like to hear", "i'd like to know", "i would like to know", "i'm curious",
    "i am curious", "i want to know", "i'd love to hear", "i would love to hear", "i'd love to understand",
    "help me understand", "please introduce", "introduce yourself", "go ahead and", "elaborate",
    "list ", "compare", "define ", "estimate", "calculate", "solve", "let's start with your",
    "let's talk about", "lets talk about",
)
FOLLOWUP = (
    "why", "why?", "why not", "how so", "how come", "elaborate", "can you elaborate", "could you elaborate",
    "tell me more", "say more", "go deeper", "dig deeper", "what about", "how about", "and then", "what else",
    "for example", "such as", "what was the result", "what was the outcome", "what did you learn",
    "what would you do differently", "what was your role", "what was the impact", "how did you measure",
    "how did that", "and why", "and how", "anything else", "what happened next", "what if", "then",
)
# Hinglish cues (Indian interviews often mix Hindi and English).
HINGLISH = ("batao", "bataiye", "bataye", "bataoge", "kya", "kyun", "kyon", "kaise", "kitna", "kitni", "kitne",
            "samjhao", "samjhaiye", "kab", "kahan", "kaun")
MULTI = (" and also ", "two things", "a couple of things", "couple of questions", "two questions", "first,",
         "secondly", "and second", "as well as", "followed by")

TYPES = (  # first match wins
    ("intro", ("tell me about yourself", "tell us about yourself", "introduce yourself", "walk me through your "
               "resume", "walk me through your cv", "walk me through your background", "your background",
               "brief introduction", "about yourself")),
    ("coding", ("write a function", "write code", "write a program", "algorithm", "time complexity",
                "space complexity", "big o", "linked list", "binary tree", "array", "string reversal",
                "reverse a", "leetcode", "data structure", "implement a", "implement the", "recursion",
                "dynamic programming", "sort ", "sorting", "hash map", "hashmap")),
    ("system_design", ("design a", "design an", "design the", "architecture", "architect", "scale to",
                       "scalab", "million users", "distributed", "high availability", "load balanc",
                       "rate limiter", "url shortener", "shard", "throughput", "latency budget")),
    ("hr", ("salary", "ctc", "compensation", "notice period", "expected pay", "expectation", "relocat",
            "why do you want", "why should we hire", "where do you see yourself", "strength",
            "weakness", "why are you leaving", "why did you leave", "join", "questions for us",
            "questions for me", "work from office", "hybrid", "shift", "career goal", "five years")),
    ("behavioral", ("tell me about a time", "tell us about a time", "describe a time", "describe a situation",
                    "give me an example of", "a time when", "a time you", "conflict", "disagree", "failure",
                    "failed", "mistake", "challenge you faced", "challenging", "proud", "difficult",
                    "teammate", "your manager", "deadline", "pressure", "feedback", "lead a team", "led a team",
                    "an example of", "mentor")),
    ("situational", ("what would you do if", "how would you handle", "imagine", "suppose", "let's say",
                     "lets say", "what if", "in a situation where")),
)
TECH_HINTS = ("python", "java", "javascript", "react", "sql", "database", "api", "rest", "http", "docker",
              "kubernetes", "aws", "cloud", "cache", "thread", "async", "memory", "index", "query", "framework",
              "microservice", "test", "git", "linux", "network", "security", "oauth", "jwt", "kafka", "redis",
              "difference between", "how does", "how do", "what is", "what are", "postgres", "mongodb",
              "browser", "url", "choose")


@dataclass(frozen=True)
class GateResult:
    decision: str             # yes | no | unsure
    text: str                 # cleaned question text
    qtype: str = "general"
    is_followup: bool = False
    multipart: bool = False
    hard: bool = False
    terminal: bool = False

    @property
    def is_question(self) -> bool:
        return self.decision == "yes"


def normalize(text: str) -> str:
    t = _SPACE.sub(" ", (text or "").strip().lower())
    t = _LEAD.sub("", t)
    return t.strip(" ,.")


def _starts(t: str, prefixes) -> bool:
    return any(t == p.strip() or t.startswith(p if p.endswith(" ") else p + " ") or t.startswith(p + ",")
               or t.startswith(p + "?") for p in prefixes)


def _qtype(t: str) -> str:
    for name, words in TYPES:
        if any(w in t for w in words):
            return name
    if any(h in t for h in TECH_HINTS):
        return "technical"
    return "general"


def classify(text: str, *, since_last_card_s: float | None = None) -> GateResult:
    raw = (text or "").strip()
    t = normalize(raw)
    words = t.split()
    if not words:
        return GateResult("no", raw)
    recent = since_last_card_s is not None and since_last_card_s <= FOLLOWUP_WINDOW_S
    asks = raw.count("?")
    if any(p in t for p in SMALL_TALK) and len(words) <= 12:
        return GateResult("no", raw)

    is_followup = recent and len(words) <= 12 and (_starts(t, FOLLOWUP) or t.rstrip("?") in FOLLOWUP)
    cue = (asks > 0 or _starts(t, WH) or _starts(t, AUX) or any(t.startswith(p) or f" {p}" in f" {t}"
                                                                for p in IMPERATIVE)
           or any(w.strip("?,.") in HINGLISH for w in words))
    if not cue and not is_followup:
        addressed = len(words) >= 6 and any(w in ("you", "your", "you've", "you're", "yourself") for w in words)
        return GateResult("unsure" if addressed else "no", raw)
    if len(words) < 2 and not is_followup:
        return GateResult("no", raw)  # a lone "what?" / "how?" with no card to follow

    qtype = _qtype(t)
    multipart = asks >= 2 or any(m in f" {t} " for m in MULTI)
    hard = qtype in ("coding", "system_design") or len(words) > 35
    terminal = raw.endswith("?") or (len(words) >= 8 and not t.endswith((" and", " or", " the", " a", " of",
                                                                          " to", " with", " your", " for")))
    return GateResult("yes", raw, qtype, bool(is_followup), multipart, hard, terminal)
