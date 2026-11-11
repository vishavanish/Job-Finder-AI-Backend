"""
app/services/resume_check.py
------------------------------
QA10 — can a resume reader (ATS) read this file, and does it fit a job?
No AI: everything here is rules over the file, so it is free, instant and
works under Free tools only.

`check(content_type, data, parsed)` -> a result dict stored in
`resumes.ats_check`:
    {version, score, band, findings: [{id, level, title, detail, fix}],
     pages, words, seen_text}
`level` is pass / info / warn / fail. No ATS really "scores" a resume
(they parse it into fields); the score is our own summary of the findings.

`keyword_fit(...)` compares a job post with a resume: hard skills from
`app/data/skill_terms.json` found / missing, the job title, education.

The resume text and the "seen text" are never logged.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("job_finder_api.resume_check")

CHECK_VERSION = 1
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SEEN_TEXT_CHARS = 6000
GOOD_FIT = 75  # % of the job's keywords — the common target (Jobscan: 75 %)

PENALTY = {"fail": 25, "warn": 8, "info": 0, "pass": 0}
# Warnings that hurt more than the rest: text read in the wrong order is the most common failure.
HEAVY_WARN = {"columns": 20, "text_boxes": 15}
BANDS = ((85, "good"), (60, "needs_fixes"), (0, "hard_to_read"))

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d ().-]{8,}\d)(?!\d)")
ODD_RE = re.compile(r"\(cid:\d+\)|�|[-]")

SECTIONS = {
    "experience": ("experience", "work experience", "professional experience", "employment", "employment history",
                   "work history", "career history", "relevant experience", "internships", "internship"),
    "education": ("education", "academic background", "academics", "qualifications", "educational qualifications",
                  "education and training", "academic qualifications"),
    "skills": ("skills", "technical skills", "key skills", "core skills", "core competencies", "skills and tools",
               "technologies", "tech stack", "skills & tools", "competencies", "areas of expertise"),
}

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RE = re.compile(rf"^\s*(?:(?:{_MONTHS})[a-z]*\.?,?\s*['’]?\d{{2,4}}|\d{{1,2}}\s*[/.-]\s*\d{{2,4}}|(?:19|20)\d{{2}}|"
                     rf"q[1-4]\s*(?:19|20)\d{{2}})\s*$", re.I)
PRESENT_RE = re.compile(r"^\s*(present|current|now|till date|to date|ongoing|today)\s*$", re.I)


# ---------------------------------------------------------------- helpers

def _finding(fid: str, level: str, title: str, detail: str = "", fix: str = "") -> dict:
    return {"id": fid, "level": level, "title": title, "detail": detail, "fix": fix}


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def _has_contact(text: str) -> tuple[bool, bool]:
    return bool(EMAIL_RE.search(text or "")), bool(PHONE_RE.search(text or ""))


def _norm_line(line: str) -> str:
    return re.sub(r"[^a-z& ]+", "", line.lower()).strip()


def found_sections(text: str) -> set[str]:
    """Standard section names written as a short line of their own."""
    out = set()
    for raw in (text or "").splitlines():
        line = _norm_line(raw)
        if not line or len(line.split()) > 5:
            continue
        for key, names in SECTIONS.items():
            if line in names:
                out.add(key)
    return out


def readable_date(value: str) -> bool:
    return bool(value) and bool(DATE_RE.match(value) or PRESENT_RE.match(value))


def _score(findings: list[dict]) -> tuple[int, str]:
    if any(f["id"] == "text_layer" and f["level"] == "fail" for f in findings):
        return 0, "hard_to_read"
    score = max(0, 100 - sum(HEAVY_WARN.get(f["id"], 8) if f["level"] == "warn" else PENALTY[f["level"]]
                             for f in findings))
    band = next(name for floor, name in BANDS if score >= floor)
    return score, band


# ---------------------------------------------------------------- PDF

def _pdf_layout(data: bytes) -> list[dict]:
    """Per page: size, text lines with positions (pdfminer reading order),
    and whether it holds pictures."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTFigure, LTImage, LTTextBox, LTTextLine

    pages = []
    for layout in extract_pages(io.BytesIO(data), laparams=LAParams()):
        lines: list[dict] = []
        images = 0

        def walk(obj) -> None:
            nonlocal images
            if isinstance(obj, (LTImage,)):
                images += 1
                return
            if isinstance(obj, LTTextBox):
                for line in obj:
                    if isinstance(line, LTTextLine):
                        text = line.get_text().strip()
                        if text:
                            lines.append({"text": text, "x0": line.x0, "x1": line.x1, "y0": line.y0, "y1": line.y1})
                return
            if isinstance(obj, LTFigure) or hasattr(obj, "__iter__"):
                for child in obj:
                    walk(child)

        for obj in layout:
            walk(obj)
        pages.append({"width": layout.width, "height": layout.height, "lines": lines, "images": images})
    return pages


def has_columns(page: dict) -> bool:
    """Two columns (or a sidebar): a vertical gap that most lines don't
    cross, with real text on both sides. Right-aligned dates on a one-column
    page don't count — they are too few words."""
    lines, width = page["lines"], page["width"] or 1
    total = sum(_words(ln["text"]) for ln in lines)
    if total < 60 or len(lines) < 10:
        return False
    for frac in range(25, 76, 2):
        gutter = width * frac / 100
        left = [ln for ln in lines if ln["x1"] <= gutter]
        right = [ln for ln in lines if ln["x0"] >= gutter]
        cross = total - sum(_words(ln["text"]) for ln in left + right)
        lw, rw = sum(_words(ln["text"]) for ln in left), sum(_words(ln["text"]) for ln in right)
        if (cross <= total * 0.15 and lw >= total * 0.15 and rw >= total * 0.15
                and len(left) >= 5 and len(right) >= 5 and _side_by_side(left, right)):
            return True
    return False


def _side_by_side(left: list[dict], right: list[dict]) -> bool:
    """Both sides share the same height band (not one block above another)."""
    def span(ls):
        return min(ln["y0"] for ln in ls), max(ln["y1"] for ln in ls)
    (l0, l1), (r0, r1) = span(left), span(right)
    overlap = min(l1, r1) - max(l0, r0)
    return overlap > 0.3 * min(l1 - l0, r1 - r0)


def _repeated_margin_lines(pages: list[dict]) -> list[str]:
    """Lines in the top or bottom 6 % of the page that repeat on every page
    — a page header/footer. Only meaningful with two or more pages."""
    if len(pages) < 2:
        return []
    per_page = []
    for p in pages:
        band = p["height"] * 0.06
        per_page.append({ln["text"] for ln in p["lines"] if ln["y0"] >= p["height"] - band or ln["y1"] <= band})
    common = set.intersection(*per_page) if per_page else set()
    return sorted(common)


def _check_pdf(data: bytes) -> tuple[list[dict], str, int]:
    findings: list[dict] = []
    pages = _pdf_layout(data)
    seen = "\n".join("\n".join(ln["text"] for ln in p["lines"]) for p in pages).strip()

    picture_pages = [i + 1 for i, p in enumerate(pages)
                     if sum(len(ln["text"]) for ln in p["lines"]) < 200 and p["images"]]
    if not seen:
        findings.append(_finding("text_layer", "fail", "No text a resume reader can read",
                                 "The file is a picture of text (scanned, or exported as an image).",
                                 "Export it again from Word or Google Docs as a PDF, or upload the .docx."))
    elif picture_pages:
        findings.append(_finding("text_layer", "warn", "Some pages are pictures",
                                 f"Page {', '.join(map(str, picture_pages))} has almost no readable text.",
                                 "Put that content in as real text, not an image."))
    else:
        findings.append(_finding("text_layer", "pass", "Real text on every page"))

    column_pages = [i + 1 for i, p in enumerate(pages) if has_columns(p)]
    if column_pages:
        findings.append(_finding("columns", "warn", "Two columns or a sidebar",
                                 f"Page {', '.join(map(str, column_pages))} has text side by side. Many readers go "
                                 "across the page, mixing the two columns.",
                                 "Use one column. Put skills and contact details in the main column."))
    elif seen:
        findings.append(_finding("columns", "pass", "One column — reads in the right order"))

    margin = _repeated_margin_lines(pages)
    body_lines = [ln["text"] for p in pages for ln in p["lines"] if ln["text"] not in margin]
    return findings + _contact_findings("\n".join(body_lines), "\n".join(margin), "page header or footer"), seen, len(pages)


# ---------------------------------------------------------------- DOCX

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _xml_text(xml: bytes) -> str:
    from lxml import etree
    root = etree.fromstring(xml)
    paras = []
    for p in root.iter(f"{W}p"):
        paras.append("".join(t.text or "" for t in p.iter(f"{W}t")))
    return "\n".join(x for x in paras if x.strip())


def _docx_body(doc) -> tuple[str, list[int]]:
    """Body text in document order (paragraphs and tables), without text
    boxes, headers or footers — what simple readers get. Also each
    table's largest cell word count."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parts, tables = [], []
    for child in doc.element.body.iterchildren():
        if child.tag == f"{W}p":
            parts.append(Paragraph(child, doc).text)
        elif child.tag == f"{W}tbl":
            table = Table(child, doc)
            biggest = 0
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    if cell.text not in cells:
                        cells.append(cell.text)
                    biggest = max(biggest, _words(cell.text))
                parts.append(" | ".join(cells))
            tables.append(biggest)
    return "\n".join(x for x in parts if x.strip()), tables


def _check_docx(data: bytes) -> tuple[list[dict], str, int | None]:
    from docx import Document
    from lxml import etree

    findings: list[dict] = []
    doc = Document(io.BytesIO(data))
    body, tables = _docx_body(doc)

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        document_xml = z.read("word/document.xml")
        margin_text = "\n".join(_xml_text(z.read(n)) for n in names
                                if re.fullmatch(r"word/(header|footer)\d*\.xml", n))
        media = [n for n in names if n.startswith("word/media/")]

    root = etree.fromstring(document_xml)
    box_text = "\n".join("".join(t.text or "" for t in box.iter(f"{W}t"))
                         for box in root.iter(f"{W}txbxContent"))
    # The same box is often stored twice (modern + fallback drawing); count words once.
    box_words = _words(box_text) // (2 if root.find(f".//{{*}}Fallback") is not None else 1)
    columns = [int(c.get(f"{W}num") or 1) for c in root.iter(f"{W}cols")]

    if not body.strip() and not box_text.strip():
        findings.append(_finding("text_layer", "fail", "No text a resume reader can read",
                                 "The document holds pictures, not text." if media else "The document is empty.",
                                 "Type the content as real text."))
    else:
        findings.append(_finding("text_layer", "pass", "Real text"))

    if any(n > 1 for n in columns):
        findings.append(_finding("columns", "warn", "Page set to two columns",
                                 "Readers often mix the two columns line by line.",
                                 "Layout → Columns → One."))
    elif tables and max(tables) > 40:
        findings.append(_finding("columns", "warn", "A table is used for the page layout",
                                 "Long text inside table cells is often read cell by cell across the row, "
                                 "or skipped (Workday is known for this).",
                                 "Take the text out of the table and use normal headings and bullets."))
    elif body.strip():
        findings.append(_finding("columns", "pass", "One column — reads in the right order"))
    if tables and max(tables) <= 40:
        findings.append(_finding("tables", "info", "Small table",
                                 "Most readers manage small tables, but some read them out of order.",
                                 "If you can, write the same content as a plain list."))

    if box_words:
        findings.append(_finding("text_boxes", "fail" if box_words >= 40 else "warn",
                                 "Text inside text boxes",
                                 f"About {box_words} words sit in text boxes or shapes. Many readers skip them.",
                                 "Move that text into the normal page (Cut, then paste outside the box)."))

    findings += _contact_findings(body, margin_text + "\n" + box_text, "page header, footer or a text box")
    return findings, body.strip(), None


# ---------------------------------------------------------------- shared checks

def _contact_findings(body: str, hidden: str, where: str) -> list[dict]:
    email, phone = _has_contact(body)
    h_email, h_phone = _has_contact(hidden)
    if email and phone:
        return [_finding("contact", "pass", "Email and phone are in the main text")]
    missing = [name for name, ok in (("email", email), ("phone number", phone)) if not ok]
    hidden_only = [name for name, ok, h in (("email", email, h_email), ("phone number", phone, h_phone)) if not ok and h]
    if hidden_only:
        return [_finding("contact", "fail", f"Your {' and '.join(hidden_only)} is only in the {where}",
                         "Many readers skip that part of the page, so recruiters may not get your details.",
                         "Put your email and phone in the first lines of the page itself.")]
    if not body.strip():
        return []
    return [_finding("contact", "warn", f"No {' or '.join(missing)} found",
                     "A recruiter needs a way to reach you.", "Add them at the top of the page.")]


def _heading_findings(text: str) -> list[dict]:
    if not text.strip():
        return []
    found = found_sections(text)
    missing = [k for k in ("experience", "education", "skills") if k not in found]
    if not missing:
        return [_finding("headings", "pass", "Standard section headings (Experience, Education, Skills)")]
    names = {"experience": "Experience", "education": "Education", "skills": "Skills"}
    return [_finding("headings", "warn", f"No “{names[k]}” heading", "Readers use standard headings to find each part.",
                     f"Name that section “{names[k]}” (or “Work Experience”, “Technical Skills”).") for k in missing]


def parsed_findings(parsed: dict | None, words: int, pages: int | None) -> list[dict]:
    """The checks that use the parsed resume (dates, length for the years).
    Re-run alone when the user saves a reviewed parse."""
    out: list[dict] = []
    years = None
    if parsed:
        years = parsed.get("total_experience_years")
        bad = []
        for e in parsed.get("experience") or []:
            start_ok = readable_date(e.get("start", ""))
            end_ok = e.get("current") or readable_date(e.get("end", ""))
            if not (start_ok and end_ok):
                bad.append(" — ".join(x for x in (e.get("title"), e.get("company")) if x) or "a job")
        if bad:
            out.append(_finding("dates", "warn", "Some job dates are hard to read",
                                f"Check: {'; '.join(bad[:4])}.",
                                "Write each job as “Jan 2022 – Present” or “03/2021 – 12/2023”, start and end."))
        elif parsed.get("experience"):
            out.append(_finding("dates", "pass", "Job dates are clear"))
    if words and words < 200:
        out.append(_finding("length", "warn", "Very short", f"About {words} words.",
                            "Add your projects, results and skills — most resumes have 400–800 words."))
    elif pages and (pages > 3 or (pages > 2 and years is not None and years < 5)):
        out.append(_finding("length", "warn", f"{pages} pages",
                            "Recruiters skim; with under 5 years of experience one or two pages is usual.",
                            "Keep the last 5–7 years in detail and shorten older work."))
    elif words:
        out.append(_finding("length", "pass", "Length is fine"))
    return out


def _file_findings(file_name: str, size: int) -> list[dict]:
    out = []
    if size > 2 * 1024 * 1024:
        out.append(_finding("file", "info", "Large file", f"{size // 1024} KB.",
                            "Some job sites limit uploads to 2 MB; export again without big images."))
    if re.search(r"\(\d+\)|final|copy|untitled|document\d*\.|new\b", file_name or "", re.I):
        out.append(_finding("file", "info", "File name", f"“{file_name}” is what recruiters see.",
                            "Name it like “Firstname-Lastname-Resume.pdf”."))
    return out


def check(content_type: str, data: bytes, *, parsed: dict | None = None, file_name: str = "") -> dict:
    if content_type == PDF:
        findings, seen, pages = _check_pdf(data)
    else:
        findings, seen, pages = _check_docx(data)
    odd = len(ODD_RE.findall(seen))
    if seen and odd:
        share = odd / max(1, len(seen))
        if share > 0.005:
            findings.append(_finding("odd_characters", "fail" if share > 0.03 else "warn", "Odd characters",
                                     f"{odd} characters come out as boxes or codes — usually icon fonts, "
                                     "fancy bullets or a font without proper text mapping.",
                                     "Use plain bullets (•) and a common font (Calibri, Arial); "
                                     "write “Email:” instead of an icon."))
    elif seen:
        findings.append(_finding("odd_characters", "pass", "No broken characters"))
    words = _words(seen)
    findings += _heading_findings(seen)
    findings += parsed_findings(parsed, words, pages)
    findings += _file_findings(file_name, len(data))
    score, band = _score(findings)
    return {"version": CHECK_VERSION, "score": score, "band": band, "findings": findings,
            "pages": pages, "words": words, "seen_text": seen[:SEEN_TEXT_CHARS],
            "checked_at": datetime.now(timezone.utc).isoformat()}


def refresh_parsed(result: dict | None, parsed: dict | None) -> dict | None:
    """After the user saves a reviewed parse: redo the dates/length part."""
    if not result or result.get("version") != CHECK_VERSION or "findings" not in result:
        return result
    kept = [f for f in result["findings"] if f["id"] not in ("dates", "length")]
    files = [f for f in kept if f["id"] == "file"]
    kept = [f for f in kept if f["id"] != "file"]
    findings = kept + parsed_findings(parsed, result.get("words") or 0, result.get("pages")) + files
    score, band = _score(findings)
    return {**result, "findings": findings, "score": score, "band": band}


def failed(message: str) -> dict:
    return {"version": CHECK_VERSION, "score": None, "band": None, "error": message, "findings": [],
            "pages": None, "words": 0, "seen_text": "", "checked_at": datetime.now(timezone.utc).isoformat()}


def is_current(result: dict | None) -> bool:
    return bool(result) and result.get("version") == CHECK_VERSION


# ---------------------------------------------------------------- keyword fit

@lru_cache(maxsize=1)
def skill_terms() -> tuple[dict[str, list[str]], list[str]]:
    path = Path(__file__).resolve().parent.parent / "data" / "skill_terms.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: [k, *v] for k, v in data["hard"].items()}, list(data["soft"])


def _contains(text: str, term: str) -> bool:
    from app.services.search_profiles import contains
    return contains(text, term)


DEGREES = (
    (3, ("phd", "ph.d", "doctorate")),
    (2, ("master's", "masters", "master of", "m.tech", "mtech", "m.e.", "m.sc", "msc", "mca", "mba", "m.s.")),
    (1, ("bachelor", "b.tech", "btech", "b.e.", "b.sc", "bsc", "bca", "b.com", "graduate degree", "undergraduate degree",
         "engineering degree", "degree in")),
)
DEGREE_NAMES = {3: "PhD", 2: "Master's", 1: "Bachelor's"}


def _degree_level(text: str) -> int:
    for level, words in DEGREES:
        if any(_contains(text, w) for w in words):
            return level
    return 0


def keyword_fit(*, job_title: str, job_text: str, resume_text: str, resume_titles: list[str] | None = None,
                missing_required: list[str] | None = None) -> dict:
    hard, soft = skill_terms()
    job = f"{job_title}\n{job_text}".lower()
    resume = (resume_text or "").lower()

    hard_found, hard_missing = [], []
    for name, terms in hard.items():
        if any(_contains(job, t) for t in terms):
            (hard_found if any(_contains(resume, t) for t in terms) else hard_missing).append(name)
    known = {t for n in hard_found + hard_missing for t in hard[n]}
    for extra in missing_required or []:  # the scorer's "asks for" list, when it isn't in the dictionary
        term = extra.strip().lower()
        if term and term not in known and not _contains(resume, term):
            hard_missing.append(term)
            known.add(term)
    soft_found = [s for s in soft if _contains(job, s) and _contains(resume, s)]
    soft_missing = [s for s in soft if _contains(job, s) and not _contains(resume, s)]

    from app.services.search_profiles import title_fit
    titles = [t for t in (resume_titles or []) if t]
    fit_t = title_fit(job_title, titles) if titles else None
    title_found = (fit_t or 0) >= 60 or (bool(job_title) and _contains(resume, job_title.lower()))

    need = _degree_level(job)
    have = _degree_level(resume)
    education = {"required": DEGREE_NAMES.get(need), "met": (have >= need) if need else None}

    parts = []
    if hard_found or hard_missing:
        parts.append((70, len(hard_found) / (len(hard_found) + len(hard_missing))))
    if job_title:
        parts.append((20, 1.0 if title_found else 0.0))
    if need:
        parts.append((10, 1.0 if education["met"] else 0.0))
    total = sum(w for w, _ in parts)
    fit = round(sum(w * v for w, v in parts) / total * 100) if total else None
    return {"fit": fit, "target": GOOD_FIT, "hard_found": hard_found, "hard_missing": hard_missing,
            "soft_found": soft_found, "soft_missing": soft_missing, "title_found": title_found,
            "education": education}


def resume_titles(parsed: dict | None) -> list[str]:
    if not parsed:
        return []
    return [parsed.get("headline") or ""] + [e.get("title") or "" for e in parsed.get("experience") or []]

