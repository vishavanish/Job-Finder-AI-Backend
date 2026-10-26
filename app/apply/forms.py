"""
app/apply/forms.py
--------------------
Reading and filling HTML application forms with Playwright (P4 plan §3.3).
Shared by every adapter; an adapter only says where the form is, what the
submit button looks like and what success looks like.

extract(page, container) returns the visible form controls as plain dicts:
  {key, label, type, options, required, name, el_id}
  type: text | textarea | number | select | radio | checkbox | combobox | file
`key` is the control's id or name — stable across page loads, so a plan
made in one browser session can be filled in another.

fill(page, container, fields, values, files) fills by key and returns the
problems it hit (never raises for one bad field). Hard stops are detected,
never worked around: login walls and captcha challenges hand the run to
the user (CLAUDE.md 7.7, §12).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("job_finder_api.apply.forms")

# Runs in the page. Marks each control with data-jf so Python can find it again in this page load.
_EXTRACT_JS = r"""
(container) => {
  const root = document.querySelector(container) || document;
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (el.type === 'file') {  // often hidden behind a styled button; not when its whole section is hidden
      const p = el.parentElement ? el.parentElement.getBoundingClientRect() : null;
      return !p || p.width > 0 || p.height > 0;
    }
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const labelOf = (el) => {
    // Upload widgets (Greenhouse) label the group, and the input's own label is just "Attach".
    const group = el.type === 'file' ? el.closest('[role="group"][aria-labelledby]') : null;
    if (group) {
      const g = document.getElementById(group.getAttribute('aria-labelledby'));
      if (g && clean(g.innerText)) return clean(g.innerText);
    }
    if (el.id) {
      const l = root.querySelector(`label[for="${CSS.escape(el.id)}"]`) || document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(n => n.innerText).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const wrap = el.closest('label');
    if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    return questionText(el, [...GROUPS, 'li']) || clean(el.getAttribute('placeholder') || el.name || '');
  };
  // The question around a control: nearest question container first, label-like elements in priority order
  // (a Lever radio sits in its own <li><label>, which must not win over the question's .application-label).
  const questionText = (el, containers) => {
    for (const cs of containers) {
      const c = el.closest(cs);
      if (!c) continue;
      for (const ls of ['legend', '.application-label', '.question-label', '.label', ':scope > label', 'label']) {
        const l = c.querySelector(ls);
        if (l && !l.contains(el) && clean(l.innerText)) return clean(l.innerText);
      }
    }
    return '';
  };
  const GROUPS = ['fieldset', '.application-question', '[role="radiogroup"]', '[role="group"]', '.field-wrapper',
                  '.field', '.form-group', '.question'];
  const requiredOf = (el, label) => !!(el.required || el.getAttribute('aria-required') === 'true'
      || /[*✱](\s|$)|\(required\)/i.test(label)
      || (el.closest('.required, [data-required="true"], [aria-required="true"]') !== null));
  // Marks from an earlier read (a previous page of a multi-page form) would make two controls share one mark.
  document.querySelectorAll('[data-jf]').forEach(e => e.removeAttribute('data-jf'));
  const out = [];
  const groups = {};
  let n = 0;
  root.querySelectorAll('input, textarea, select').forEach((el) => {
    const t = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(t)) return;
    if (!visible(el)) return;
    if (el.closest('.iti__dropdown-content, .iti__country-list')) return;  // phone widget's country search
    if (el.getAttribute('aria-hidden') === 'true' && t !== 'file') return;
    const mark = 'f' + (n++);
    el.setAttribute('data-jf', mark);
    if (t === 'radio' || (t === 'checkbox' && el.name && root.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const gkey = t + ':' + (el.name || mark);
      if (!groups[gkey]) {
        groups[gkey] = {key: el.name || mark, name: el.name || '', el_id: '', type: t === 'radio' ? 'radio' : 'checkbox_group',
                        label: questionText(el, GROUPS) || clean(el.name), options: [], marks: [], required: false};
        out.push(groups[gkey]);
      }
      const g = groups[gkey];
      const optLabel = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) ? clean(document.querySelector(`label[for="${CSS.escape(el.id)}"]`).innerText)
                     : clean((el.closest('label') || {}).innerText || el.value);
      g.options.push(optLabel); g.marks.push(mark);
      g.required = g.required || requiredOf(el, g.label);
      return;
    }
    const label = labelOf(el);
    let type = el.tagName === 'TEXTAREA' ? 'textarea' : el.tagName === 'SELECT' ? 'select'
             : t === 'file' ? 'file' : t === 'checkbox' ? 'checkbox' : t === 'number' ? 'number'
             : (el.getAttribute('role') === 'combobox' ? 'combobox' : 'text');
    const options = type === 'select' ? Array.from(el.options).filter(o => o.value !== '' && clean(o.text) && !/^(select|choose|please select|--)/i.test(clean(o.text))).map(o => clean(o.text)) : [];
    out.push({key: el.id || el.name || mark, name: el.name || '', el_id: el.id || '', type, label, options,
              marks: [mark], required: requiredOf(el, label), accept: el.getAttribute('accept') || ''});
  });
  return out;
}
"""

# An open puzzle (after submitting, or a page-level block). These frames are often in the DOM but hidden.
_CHALLENGE_SELECTORS = (
    'iframe[src*="recaptcha"][src*="bframe"]', 'iframe[title*="hCaptcha challenge" i]',
    'iframe[src*="hcaptcha"][src*="frame=challenge"]', 'iframe[src*="challenges.cloudflare.com"]',
)
# An "I am human" checkbox on the form itself: a person has to tick it, so the form can't be submitted for them.
_HUMAN_CHECKBOX_SELECTORS = (
    'iframe[title*="checkbox for hCaptcha" i]', 'iframe[src*="hcaptcha"][src*="frame=checkbox"]',
    'iframe[src*="recaptcha"][src*="anchor"]:not([src*="size=invisible"])',
)
_HUMAN_CHECK_TEXT = re.compile(r"verify (that )?you are (a )?human|are you a robot|complete the captcha|"
                               r"press (and|&) hold", re.I)
# A site refusing the server itself (QA5): nothing on the page to solve, so it is never worked around.
_BLOCK_TEXT = re.compile(
    r"access (is )?(temporarily )?restricted|unusual (activity|traffic) from your (device|network|computer)|"
    r"automated \(?bot\)? activity|(sorry, )?you have been blocked|request (was |has been )?blocked|"
    r"access denied.{0,300}reference ?#|attention required.{0,300}cloudflare", re.I | re.S)
_BLOCK_STATUS = {403, 429}
_BLOCK_PAGE_MAX_CHARS = 3000  # block pages are short; a long job post that mentions "blocked" is not one
SUCCESS_TEXT = re.compile(
    r"thank(s| you) for (applying|your application|your interest)|application (has been |was )?(submitted|received)|"
    r"we('ve| have) received your application|your application (has been|was) (sent|submitted|received)|"
    r"successfully (applied|submitted)", re.I)
ERROR_TEXT = re.compile(r"(this field is|is) required|please (fill|complete|correct|enter)|"
                        r"there (was|were) (an )?errors?|invalid", re.I)


@dataclass
class Outcome:
    state: str           # submitted | captcha | invalid | unknown
    text: str = ""


def extract(page, container: str) -> list[dict]:
    """Visible controls in `container` (CSS selector; whole page if absent).
    Combobox options are read by opening each one."""
    fields = page.evaluate(_EXTRACT_JS, container)
    for f in fields:
        if f["type"] == "combobox":
            f["options"] = _combobox_options(page, f["marks"][0])
        f["type"] = {"checkbox_group": "checkbox"}.get(f["type"], f["type"])
    return fields


def _combobox_options(page, mark: str) -> list[str]:
    """Opens the combobox and reads ITS listbox (aria-controls/aria-owns),
    else the options that are visible now — never every option on the page."""
    loc = page.locator(f'[data-jf="{mark}"]')
    try:
        loc.click(timeout=3000)
        page.wait_for_timeout(400)
        listbox = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
        opts = (page.locator(f'[id="{listbox}"] [role="option"]') if listbox
                else page.locator('[role="option"]:visible'))
        options = [o.strip() for o in opts.all_inner_texts() if o.strip()]
        page.keyboard.press("Escape")
        return options[:200]
    except Exception:  # noqa: BLE001 — unknown options just mean the user may be asked
        return []


def for_answering(fields: list[dict]) -> list[dict]:
    """Fields in the shape answer_bank.answer_fields expects (files excluded)."""
    out = []
    for f in fields:
        if f["type"] == "file":
            continue
        if f["type"] in ("select", "radio", "combobox"):
            ftype = "select"
        elif f["type"] == "checkbox" and len(f.get("options") or []) <= 1:
            ftype, f = "yes_no", {**f, "options": ["Yes", "No"]}
        else:
            ftype = {"textarea": "textarea", "number": "number"}.get(f["type"], "text")
        out.append({"id": f["key"], "label": f["label"], "type": ftype, "options": f.get("options") or [],
                    "required": f["required"]})
    return out


def login_wall(page) -> bool:
    try:
        return page.locator('input[type="password"]:visible').count() > 0
    except Exception:  # noqa: BLE001
        return False


def _any_shown(page, selectors: tuple[str, ...], min_size: int) -> bool:
    for sel in selectors:
        try:
            frames = page.locator(sel)
            for i in range(frames.count()):
                frame = frames.nth(i)
                if not frame.is_visible():
                    continue
                box = frame.bounding_box()
                if box and box["width"] >= min_size and box["height"] >= min_size and box["y"] > -500:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def human_checkbox(page) -> bool:
    """The form has a visible "I am human" captcha checkbox."""
    return _any_shown(page, _HUMAN_CHECKBOX_SELECTORS, 30)


def challenge_visible(page) -> bool:
    if _any_shown(page, _CHALLENGE_SELECTORS, 50):
        return True
    try:
        return bool(_HUMAN_CHECK_TEXT.search(page.inner_text("body", timeout=2000)))
    except Exception:  # noqa: BLE001
        return False


def blocked(page, status: int | None = None) -> str | None:
    """Why the site refused the browser (a bot / access block page), or None. `status` is the main
    document's HTTP status when the caller has it."""
    if status in _BLOCK_STATUS:
        return f"The site answered HTTP {status}."
    try:
        text = page.inner_text("body", timeout=2000)
    except Exception:  # noqa: BLE001
        return None
    if len(text) > _BLOCK_PAGE_MAX_CHARS:
        return None
    m = _BLOCK_TEXT.search(text)
    if m is None:
        return None
    said = " ".join(m.group(0).split())[:80]
    return f'The page says "{said}".'


def _match(fields: list[dict], key: str) -> dict | None:
    return next((f for f in fields if f["key"] == key), None)


def _choose(options: list[str], value: str) -> int | None:
    v = (value or "").strip().lower()
    for i, o in enumerate(options):
        if o.strip().lower() == v:
            return i
    hits = [i for i, o in enumerate(options) if v and (v in o.lower() or o.strip().lower() in v)]
    return hits[0] if len(hits) == 1 else None


def fill(page, fields: list[dict], values: dict[str, str], files: dict[str, str]) -> list[str]:
    """Fills `values` ({key: value}) and `files` ({key: path}) into the
    controls found by extract() on THIS page. Returns problems."""
    problems: list[str] = []
    for key, value in values.items():
        f = _match(fields, key)
        if f is None:
            problems.append(f"field '{key}' is not on the page any more")
            continue
        if value in (None, ""):
            continue
        try:
            _fill_one(page, f, str(value))
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not fill '{f['label'][:60]}': {type(e).__name__}")
    for key, path in files.items():
        f = _match(fields, key)
        if f is None:
            problems.append(f"file field '{key}' is not on the page any more")
            continue
        try:
            page.locator(f'[data-jf="{f["marks"][0]}"]').set_input_files(path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not upload to '{f['label'][:60]}': {type(e).__name__}")
    return problems


def _fill_one(page, f: dict, value: str) -> None:
    loc = page.locator(f'[data-jf="{f["marks"][0]}"]')
    t = f["type"]
    if t in ("text", "textarea", "number"):
        loc.fill(value)
    elif t == "select":
        try:
            loc.select_option(label=value)
        except Exception:  # noqa: BLE001
            i = _choose(f["options"], value)
            if i is None:
                raise
            loc.select_option(label=f["options"][i])
    elif t == "radio":
        i = _choose(f["options"], value)
        if i is None:
            raise ValueError("no matching option")
        page.locator(f'[data-jf="{f["marks"][i]}"]').check()
    elif t == "checkbox":
        if len(f["marks"]) > 1:
            for part in [p.strip() for p in value.split(",") if p.strip()]:
                i = _choose(f["options"], part)
                if i is None:
                    raise ValueError("no matching option")
                page.locator(f'[data-jf="{f["marks"][i]}"]').check()
        elif value.strip().lower() in ("yes", "true", "1", "on"):
            loc.check()
        else:
            loc.uncheck()
    elif t == "combobox":
        loc.click()
        loc.fill(value)
        page.wait_for_timeout(400)
        option = page.locator('[role="option"]', has_text=value).first
        if option.count() == 0:
            page.keyboard.press("Escape")
            raise ValueError("no matching option")
        option.click()
    else:
        raise ValueError(f"unsupported field type {t}")


def empty_required(page, fields: list[dict], filled_keys: set[str]) -> list[str]:
    """Labels of required fields still empty after filling."""
    return [f["label"] or f["key"] for f in fields if f["required"] and f["key"] not in filled_keys]


def outcome(page, *, wait_ms: int = 15000) -> Outcome:
    """After clicking submit: success text, a captcha challenge, form errors,
    or unknown (the caller hands the run to the user, never guesses)."""
    waited = 0
    while waited <= wait_ms:
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:  # noqa: BLE001
            body = ""
        m = SUCCESS_TEXT.search(body)
        if m or re.search(r"/(confirmation|thank[-_]?you|thanks|success)", page.url, re.I):
            start = max(0, (m.start() if m else 0) - 80)
            return Outcome("submitted", " ".join(body[start:start + 300].split()))
        if challenge_visible(page):
            return Outcome("captcha", "A captcha appeared after submitting.")
        if page.locator('[aria-invalid="true"]:visible, .error:visible, .field-error:visible').count() > 0 \
                and ERROR_TEXT.search(body):
            return Outcome("invalid", " ".join(ERROR_TEXT.search(body).group(0).split()))
        page.wait_for_timeout(1000)
        waited += 1000
    return Outcome("unknown", "No confirmation appeared after submitting.")
