"""QA10 test resumes, built in code (no real person's resume in the repo).

`make_pdf(pages)` writes a small text PDF: each page is a list of
(x, y, text) lines in points on a 612 x 792 page (Helvetica 10).
`make_docx(...)` builds DOCX variants with python-docx.
"""
from __future__ import annotations

import io

LOREM = ("Built and shipped backend services in Python and FastAPI with PostgreSQL and Redis, "
         "cut response time by forty percent and mentored two engineers")


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[tuple[float, float, str]]], *, image_only: bool = False) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids, content_ids = [], []
    pages_id_placeholder = len(objects) + 1 + 2 * len(pages) + (1 if image_only else 0)
    image = None
    if image_only:
        pixels = b"\x80" * (20 * 20)
        image = add(b"<< /Type /XObject /Subtype /Image /Width 20 /Height 20 /ColorSpace /DeviceGray "
                    b"/BitsPerComponent 8 /Length %d >>\nstream\n" % len(pixels) + pixels + b"\nendstream")
        pages_id_placeholder = len(objects) + 2 * len(pages) + 1
    for lines in pages:
        if image_only:
            stream = b"q 500 0 0 700 50 50 cm /Im1 Do Q"
        else:
            stream = b"".join(b"BT /F1 10 Tf %.1f %.1f Td (%s) Tj ET\n" % (x, y, _esc(t).encode("latin-1"))
                              for x, y, t in lines)
        content_ids.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        res = b"/Font << /F1 %d 0 R >>" % font
        if image:
            res += b" /XObject << /Im1 %d 0 R >>" % image
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << %s >> "
                            b"/Contents %d 0 R >>" % (pages_id_placeholder, res, content_ids[-1])))
    pages_id = add(b"<< /Type /Pages /Kids [%s] /Count %d >>"
                   % (b" ".join(b"%d 0 R" % i for i in page_ids), len(page_ids)))
    assert pages_id == pages_id_placeholder
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref))
    return out.getvalue()


def one_column_lines(top: float = 760) -> list[tuple[float, float, str]]:
    lines = [(50, top, "Asha Rao"), (50, top - 14, "asha.rao@example.com | +91 98765 43210 | Pune"),
             (50, top - 40, "Summary"), (50, top - 54, LOREM),
             (50, top - 80, "Experience"), (50, top - 94, "Backend Engineer, Orbit Labs        Jan 2022 - Present")]
    y = top - 108
    for _ in range(8):
        lines.append((60, y, "- " + LOREM))
        y -= 14
    lines += [(50, y - 12, "Education"), (50, y - 26, "B.Tech Computer Science, Pune University 2017 - 2021"),
              (50, y - 52, "Skills"), (50, y - 66, "Python, FastAPI, Django, PostgreSQL, Redis, Docker, AWS, LangChain, RAG")]
    y -= 80
    for _ in range(6):
        lines.append((60, y, "- " + LOREM))
        y -= 14
    return lines


def two_column_lines() -> list[tuple[float, float, str]]:
    lines = [(40, 760, "Asha Rao"), (40, 746, "asha.rao@example.com"), (40, 732, "+91 98765 43210"),
             (40, 700, "Skills")]
    y = 686
    for skill in ("Python", "FastAPI", "Django", "PostgreSQL", "Redis", "Docker", "AWS", "LangChain",
                  "RAG pipelines", "Kubernetes", "Celery workers", "Pytest suites"):
        lines.append((40, y, f"{skill} and related tools daily"))
        y -= 14
    lines += [(40, y - 10, "Education"), (40, y - 24, "B.Tech Pune University 2021")]
    lines += [(250, 760, "Experience"), (250, 746, "Backend Engineer, Orbit Labs  Jan 2022 - Present")]
    y = 732
    for _ in range(18):
        lines.append((250, y, "- shipped Python services with FastAPI and Postgres"))
        y -= 14
    return lines


def make_docx(*, contact_in_header: bool = False, text_box: bool = False, layout_table: bool = False,
              two_columns: bool = False, sections: bool = True) -> bytes:
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    contact = "asha.rao@example.com | +91 98765 43210 | Pune"
    if contact_in_header:
        doc.sections[0].header.paragraphs[0].text = "Asha Rao | " + contact
    else:
        doc.add_paragraph("Asha Rao")
        doc.add_paragraph(contact)
    heading = (lambda t: doc.add_heading(t, level=2)) if sections else (lambda t: doc.add_paragraph(t + " journey so far"))
    heading("Experience")
    doc.add_paragraph("Backend Engineer, Orbit Labs (Jan 2022 - Present)")
    for _ in range(6):
        doc.add_paragraph(LOREM, style="List Bullet")
    heading("Education")
    doc.add_paragraph("B.Tech Computer Science, Pune University (2017 - 2021)")
    heading("Skills")
    doc.add_paragraph("Python, FastAPI, Django, PostgreSQL, Redis, Docker, AWS, LangChain, RAG")
    if layout_table:
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Projects"
        table.cell(0, 1).text = " ".join([LOREM] * 3)
    if text_box:
        body = doc.element.body
        p = parse_xml(
            '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:v="urn:schemas-microsoft-com:vml"><w:r><w:pict><v:shape><v:textbox>'
            '<w:txbxContent><w:p><w:r><w:t>' + " ".join([LOREM] * 2) + '</w:t></w:r></w:p></w:txbxContent>'
            '</v:textbox></v:shape></w:pict></w:r></w:p>')
        body.insert(len(body) - 1, p)
    if two_columns:
        sect = doc.sections[0]._sectPr
        sect.append(parse_xml('<w:cols xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:num="2"/>'))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
