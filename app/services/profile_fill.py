"""
app/services/profile_fill.py
------------------------------
Fills the profile's Applicant info from the master resume (QA2 §3).

The QA pass found the resume parse knew the phone number and work history
while Applicant info — what apply forms are filled with — stayed empty. After
a master resume is parsed or reviewed, EMPTY applicant fields are filled from
it; anything the user typed is never overwritten. Pasted profile links lose
their tracking parameters (`?isSelfProfile=true`, `utm_*`...).

Years of experience: the parser's `total_experience_years`, or when it left
that out, the span of the dated jobs (overlaps counted once).
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.models.profile import Profile
from app.models.profile_schemas import ApplicantInfoIn, ParsedResume

_TRACKING = re.compile(r"^(utm_.*|trk.*|isselfprofile|originalsubdomain|fbclid|gclid|ref|refid|src|si)$",
                       re.IGNORECASE)
URL_FIELDS = ("linkedin_url", "github_url", "portfolio_url")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def clean_url(url: str) -> str:
    """Drops tracking parameters and the #fragment; keeps everything else."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _month(text: str, *, today: date) -> tuple[int, int] | None:
    """(year, month) from "09/2025", "Sep 2025", "September 2025", "2025-09",
    "2025" (January) or "Present"/"Current"/"Now" (this month)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in ("present", "current", "now", "till date", "to date", "ongoing"):
        return today.year, today.month
    if m := re.fullmatch(r"(\d{1,2})[/\-.](\d{4})", t):
        return int(m.group(2)), max(1, min(12, int(m.group(1))))
    if m := re.fullmatch(r"(\d{4})[/\-.](\d{1,2})", t):
        return int(m.group(1)), max(1, min(12, int(m.group(2))))
    if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?,?\s+(\d{4})", t):
        if m.group(1) in _MONTHS:
            return int(m.group(2)), _MONTHS[m.group(1)]
    if m := re.fullmatch(r"(\d{4})", t):
        return int(m.group(1)), 1
    return None


def experience_years(parsed: ParsedResume, *, today: date | None = None) -> float | None:
    """The parser's figure, else the months covered by dated jobs (overlaps once), in years (0.5 steps)."""
    if parsed.total_experience_years is not None:
        return parsed.total_experience_years
    today = today or date.today()
    months: set[tuple[int, int]] = set()
    for item in parsed.experience:
        start = _month(item.start, today=today)
        end = _month(item.end, today=today) if item.end else ((today.year, today.month) if item.current else None)
        if not start or not end or end < start:
            continue
        y, m = start
        while (y, m) <= end:
            months.add((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if not months:
        return None
    return round(len(months) / 12 * 2) / 2


def _name_parts(name: str) -> tuple[str, str]:
    words = (name or "").split()
    if not words:
        return "", ""
    if name.isupper():  # "AVANISH VISHWAKARMA" -> "Avanish Vishwakarma"
        words = [w.capitalize() for w in words]
    return words[0], " ".join(words[1:])


def from_resume(parsed: ParsedResume) -> dict:
    """Applicant info the resume can answer (empty values left out)."""
    first, last = _name_parts(parsed.name)
    loc = [p.strip() for p in (parsed.location or "").split(",") if p.strip()]
    out: dict = {"first_name": first, "last_name": last, "email": parsed.email.strip(),
                 "phone": parsed.phone.strip(), "city": loc[0] if loc else "",
                 "country": loc[-1] if len(loc) > 1 else ""}
    for link in parsed.links:
        url = clean_url(link.url)
        host = urlsplit(url).netloc.lower()
        if "linkedin.com" in host:
            out.setdefault("linkedin_url", url)
        elif "github.com" in host:
            out.setdefault("github_url", url)
        elif url:
            out.setdefault("portfolio_url", url)
    years = experience_years(parsed)
    if years is not None:
        out["total_experience_years"] = years
    return {k: v for k, v in out.items() if v not in ("", None)}


def fill_empty(db: Session, user_id: str, parsed: ParsedResume) -> list[str]:
    """Copies resume answers into EMPTY applicant fields. Returns the fields
    filled; the caller commits."""
    row = db.query(Profile).filter(Profile.user_id == user_id).first()
    if row is None:
        row = Profile(user_id=user_id)
        db.add(row)
    known = ApplicantInfoIn.model_fields
    current = ApplicantInfoIn.model_validate(
        {k: v for k, v in (row.applicant_info or {}).items() if k in known}).model_dump()
    filled = []
    for field, value in from_resume(parsed).items():
        if field in current and current[field] in ("", None):
            current[field] = value
            filled.append(field)
    if filled:
        row.applicant_info = ApplicantInfoIn.model_validate(current).model_dump()
    return filled


def clean_applicant_urls(info: ApplicantInfoIn) -> ApplicantInfoIn:
    """Profile links as the user saved them, minus tracking parameters."""
    return info.model_copy(update={f: clean_url(getattr(info, f)) for f in URL_FIELDS})
