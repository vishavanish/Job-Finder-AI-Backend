"""
app/services/email_templates.py
---------------------------------
HTML versions of the emails people get from the app (QA7, 2026-10-31): the job-alert digest and the
"new roles at companies you follow" digest. The plain-text versions stay (job_alerts.digest_text,
target_companies.digest_text) and go out as the text/plain part of the same message.

Built for real inboxes, not browsers: one 600 px table, every style inline (Gmail drops most <style>),
bgcolor attributes for Outlook, "bulletproof" table buttons, a hidden preheader line, system fonts, and a
@media block that only adds phone sizing and a dark palette where the client supports it (Apple Mail,
iOS, Outlook apps). Colours follow the web app's tokens (plum accent, warm graphite neutrals).

Every value from a job or a company goes through `_e()` (HTML-escaped); links must be http(s).
"""
from __future__ import annotations

import html
from datetime import datetime

from app.core.config import get_settings

# Web app tokens (job-finder-frontend/src/styles/tokens.css).
INK, MUTED, SUBTLE = "#1b1a18", "#5d5951", "#79746a"
PAGE, CARD, LINE, SUNKEN = "#f7f6f3", "#ffffff", "#e7e4dd", "#efeee9"
PLUM, PLUM_DARK, PLUM_SOFT, PLUM_LINE = "#6a3d98", "#57307e", "#f5f0fa", "#ebe1f5"
GREEN, GREEN_SOFT = "#1f7a4d", "#e7f5ee"
AMBER, AMBER_SOFT = "#8a5a00", "#fdf3df"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

_STYLE = f"""
@media only screen and (max-width:620px) {{
  .jf-container {{ width:100% !important; }}
  .jf-pad {{ padding-left:18px !important; padding-right:18px !important; }}
  .jf-hero {{ font-size:26px !important; line-height:32px !important; }}
  .jf-btn a {{ display:block !important; }}
}}
@media (prefers-color-scheme: dark) {{
  .jf-page {{ background:#141311 !important; }}
  .jf-card {{ background:#1f1e1b !important; border-color:#34312c !important; }}
  .jf-ink {{ color:#f2f0eb !important; }}
  .jf-muted {{ color:#b7b2a8 !important; }}
  .jf-soft {{ background:#2a2433 !important; color:#d9c8ef !important; }}
  .jf-link {{ color:#c9a8f0 !important; }}
}}
"""


def _e(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _url(value) -> str:
    """Only http(s) links; anything else becomes '' (no javascript:, data:, mailto: tricks)."""
    v = str(value or "").strip()
    return html.escape(v, quote=True) if v.lower().startswith(("https://", "http://")) else ""


def app_url(path: str = "") -> str:
    base = (get_settings().APP_PUBLIC_URL or "").rstrip("/")
    return f"{base}{path}" if base else ""


def _chip(text: str, *, fg: str = PLUM_DARK, bg: str = PLUM_SOFT, cls: str = "jf-soft") -> str:
    return (f'<span class="{cls}" style="display:inline-block;margin:0 6px 6px 0;padding:3px 9px;border-radius:999px;'
            f'background:{bg};color:{fg};font:600 12px/18px {FONT};white-space:nowrap;">{_e(text)}</span>')


def _button(label: str, href: str, *, primary: bool = True) -> str:
    href = _url(href)
    if not href:
        return ""
    bg, fg, border = (PLUM, "#ffffff", PLUM) if primary else (CARD, PLUM_DARK, PLUM_LINE)
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="jf-btn" '
            f'style="display:inline-table;margin:0 8px 0 0;"><tr><td bgcolor="{bg}" '
            f'style="border-radius:8px;border:1px solid {border};">'
            f'<a href="{href}" target="_blank" style="display:inline-block;padding:9px 16px;font:600 14px/20px {FONT};'
            f'color:{fg};text-decoration:none;border-radius:8px;">{_e(label)}</a></td></tr></table>')


def _score_chip(score) -> str:
    if not isinstance(score, (int, float)):
        return ""
    s = round(score)
    fg, bg = (GREEN, GREEN_SOFT) if s >= 85 else (PLUM_DARK, PLUM_SOFT) if s >= 70 else (MUTED, SUNKEN)
    return _chip(f"{s}% match", fg=fg, bg=bg, cls="")


def _layout(*, title: str, preheader: str, eyebrow: str, hero: str, intro: str, body: str, footer: str) -> str:
    return f"""<!doctype html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_e(title)}</title>
<style>{_STYLE}</style>
</head>
<body class="jf-page" style="margin:0;padding:0;background:{PAGE};-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;mso-hide:all;">{_e(preheader)}&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAGE}" class="jf-page" style="background:{PAGE};">
<tr><td align="center" style="padding:28px 12px;">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" class="jf-container" style="width:600px;max-width:600px;">
    <tr><td class="jf-pad" style="padding:0 8px 16px 8px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
        <td bgcolor="{PLUM}" style="width:30px;height:30px;border-radius:8px;background:{PLUM};text-align:center;font:700 15px/30px {FONT};color:#ffffff;">J</td>
        <td class="jf-ink" style="padding-left:10px;font:700 16px/30px {FONT};color:{INK};">Job Finder AI</td>
      </tr></table>
    </td></tr>
    <tr><td class="jf-card" bgcolor="{CARD}" style="background:{CARD};border:1px solid {LINE};border-radius:16px;overflow:hidden;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td bgcolor="{PLUM}" style="height:5px;line-height:5px;font-size:0;background:{PLUM};background-image:linear-gradient(90deg,{PLUM},#5b5bd6);">&nbsp;</td></tr>
        <tr><td class="jf-pad" style="padding:28px 32px 8px 32px;">
          <div class="jf-muted" style="font:600 12px/16px {FONT};letter-spacing:.08em;text-transform:uppercase;color:{SUBTLE};">{eyebrow}</div>
          <div class="jf-ink jf-hero" style="margin-top:8px;font:700 30px/36px {FONT};color:{INK};">{hero}</div>
          <div class="jf-muted" style="margin-top:8px;font:400 15px/22px {FONT};color:{MUTED};">{intro}</div>
        </td></tr>
        {body}
      </table>
    </td></tr>
    <tr><td class="jf-pad jf-muted" style="padding:18px 16px 0 16px;font:400 12px/18px {FONT};color:{SUBTLE};text-align:center;">{footer}</td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def _job_row(job: dict, *, last: bool) -> str:
    title, company, location = _e(job.get("title")), _e(job.get("company")), _e(job.get("location"))
    link = _url(job.get("url"))
    chips = [_score_chip(job.get("llm_score"))]
    hours = job.get("posted_hours_ago")
    if job.get("early") or (isinstance(hours, (int, float)) and hours < 24):
        chips.append(_chip("New · under 24 h", fg=GREEN, bg=GREEN_SOFT, cls=""))
    match = job.get("match") or {}
    if isinstance(match, dict) and match.get("skills_total"):
        chips.append(_chip(f"{len(match.get('skills_matched') or [])}/{match['skills_total']} skills"))
    if job.get("applicants"):
        chips.append(_chip(f"{job['applicants']} applicants", fg=AMBER, bg=AMBER_SOFT, cls=""))
    if job.get("source"):
        chips.append(_chip(str(job["source"]), fg=MUTED, bg=SUNKEN, cls=""))
    reason = _e(job.get("llm_reason"))
    head = (f'<a href="{link}" target="_blank" class="jf-ink" style="color:{INK};text-decoration:none;">{title}</a>'
            if link else title)
    where = " · ".join(x for x in (company, location) if x)
    border = "" if last else f"border-bottom:1px solid {LINE};"
    return f"""<tr><td class="jf-pad" style="padding:20px 32px;{border}">
  <div class="jf-ink" style="font:700 17px/24px {FONT};color:{INK};">{head}</div>
  <div class="jf-muted" style="margin:4px 0 10px 0;font:500 14px/20px {FONT};color:{MUTED};">{where}</div>
  <div>{''.join(c for c in chips if c)}</div>
  {f'<div class="jf-muted" style="margin:6px 0 0 0;padding:10px 12px;border-left:3px solid {PLUM_LINE};font:400 14px/21px {FONT};color:{MUTED};">{reason}</div>' if reason else ''}
  <div style="margin-top:14px;">{_button("View job", job.get("url"))}</div>
</td></tr>"""


def job_alert_digest(alert_name: str, min_score: int, jobs: list[dict]) -> str:
    n = len(jobs)
    fresh = sum(1 for j in jobs if j.get("early"))
    top = max((j.get("llm_score") for j in jobs if isinstance(j.get("llm_score"), (int, float))), default=None)
    summary = [f"{n} new match{'es' if n != 1 else ''}"]
    if fresh:
        summary.append(f"{fresh} posted in the last 24 h")
    if top is not None:
        summary.append(f"best match {round(top)}%")
    rows = "".join(_job_row(j, last=i == n - 1) for i, j in enumerate(jobs))
    open_app = app_url("/jobs")
    cta = (f'<tr><td class="jf-pad" style="padding:8px 32px 28px 32px;border-top:1px solid {LINE};">'
           f'{_button("Open all in Job Finder", open_app, primary=False)}</td></tr>') if open_app else ""
    manage = app_url("/alerts")
    footer = (f'You get this email because your job alert <b>{_e(alert_name)}</b> is on '
              f'(matches scoring {int(min_score)}+). '
              + (f'<a href="{_url(manage)}" class="jf-link" style="color:{PLUM};">Change or turn off this alert</a>.'
                 if manage else "Change or turn it off in Job Finder AI → Alerts.")
              + "<br>Scores and notes are written by AI from your resume — check the job page before you apply.")
    return _layout(title=f"{n} new job matches — {alert_name}",
                   preheader=" · ".join(summary) + f" — {alert_name}",
                   eyebrow=f"Job alert · {_e(alert_name)}",
                   hero=f"{n} new job match{'es' if n != 1 else ''} for you",
                   intro=_e(" · ".join(summary[1:]) or "Fresh roles that fit your resume."),
                   body=rows + cta, footer=footer)


def _ago(when: datetime | None, now: datetime) -> str:
    if not when:
        return ""
    hours = max(0, int((now - when).total_seconds() // 3600))
    return "just now" if hours < 1 else f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def company_roles_digest(roles: list[dict], now: datetime) -> str:
    groups: dict[str, list[dict]] = {}
    for r in roles:
        groups.setdefault(str(r.get("company") or "Company"), []).append(r)
    n, companies = len(roles), len(groups)
    blocks = []
    for gi, (company, items) in enumerate(groups.items()):
        lines = []
        for r in items:
            where = r.get("location") or ("Remote" if r.get("remote") else "")
            meta = " · ".join(x for x in (_e(where), _e(_ago(r.get("posted_at"), now))) if x)
            link = _url(r.get("url"))
            title = _e(r.get("title"))
            lines.append(
                f'<tr><td style="padding:10px 0;border-top:1px solid {LINE};">'
                f'<div class="jf-ink" style="font:600 15px/22px {FONT};color:{INK};">'
                + (f'<a href="{link}" target="_blank" class="jf-ink" style="color:{INK};text-decoration:none;">{title}</a>'
                   if link else title)
                + f'</div><div class="jf-muted" style="font:400 13px/19px {FONT};color:{MUTED};">{meta}</div></td>'
                + (f'<td align="right" valign="middle" style="padding:10px 0 10px 12px;border-top:1px solid {LINE};'
                   f'white-space:nowrap;"><a href="{link}" target="_blank" class="jf-link" '
                   f'style="font:600 13px/19px {FONT};color:{PLUM};text-decoration:none;">View&nbsp;&rarr;</a></td>'
                   if link else '<td></td>')
                + "</tr>")
        border = "" if gi == companies - 1 else f"border-bottom:1px solid {LINE};"
        blocks.append(
            f'<tr><td class="jf-pad" style="padding:18px 32px;{border}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td class="jf-ink" style="font:700 16px/22px {FONT};color:{INK};">{_e(company)}</td>'
            f'<td align="right">{_chip(f"{len(items)} new role" + ("s" if len(items) != 1 else ""))}</td></tr></table>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:6px;">'
            f'{"".join(lines)}</table></td></tr>')
    page = app_url("/ats-targets")
    cta = (f'<tr><td class="jf-pad" style="padding:8px 32px 28px 32px;border-top:1px solid {LINE};">'
           f'{_button("See your companies", page)}</td></tr>') if page else ""
    footer = ("You get this email because you follow these companies in Job Finder AI. "
              + (f'<a href="{_url(page)}" class="jf-link" style="color:{PLUM};">Change how often you hear from us</a>.'
                 if page else "Change it in Job Finder AI → Target companies."))
    return _layout(title=f"{n} new roles at companies you follow",
                   preheader=f"{n} new role{'s' if n != 1 else ''} at {', '.join(list(groups)[:3])}",
                   eyebrow="Companies you follow",
                   hero=f"{n} new role{'s' if n != 1 else ''} at {companies} compan{'ies' if companies != 1 else 'y'}",
                   intro="Fresh openings on the career pages you follow, matched to your titles and places.",
                   body="".join(blocks) + cta, footer=footer)
