"""
app/apply/adapters.py
-----------------------
Platform adapters for the apply engine (CLAUDE.md 7.7, P4 plan §3.3). An
adapter knows how to reach a platform's application form, where the form
is, how to submit it and whether it may submit at all. Reading and
filling the form is shared (app/apply/forms.py).

  greenhouse  boards.greenhouse.io / job-boards.greenhouse.io pages, or a
              company page embedding the Greenhouse iframe (gh_jid=...).
  lever       jobs.lever.co/{company}/{id} -> its /apply form.
  linkedin    Easy Apply: fill only, NEVER submits (LinkedIn's terms; the
              user clicks Submit in the visible browser on their own PC).
  generic     the AI form reader for other hosted career forms, possibly
              several pages long (app/apply/generic.py, P4c).

Adding a platform = a new class here + an entry in ADAPTERS (and in
apply_options.detect_adapter). Nothing in the engine changes.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.apply import forms


class HardStop(Exception):
    """The page needs a human: login, captcha, unsupported page. `reason`
    is shown to the user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Blocked(HardStop):
    """The site refused the server's browser (bot / access block, QA5). The engine names the platform and
    pauses Auto apply on it; the block is never worked around."""


class Adapter:
    name = ""
    can_submit = True
    multi_step = False  # True: the engine walks the form page by page (is_final / next_step)
    form_selector = "form"
    submit_selector = 'button[type="submit"], input[type="submit"]'

    def open(self, page, url: str, timeout_ms: int) -> None:
        response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        block = forms.blocked(page, getattr(response, "status", None))
        if block:
            raise Blocked(block)
        if forms.login_wall(page):
            raise HardStop("The application page asks you to sign in.")
        if forms.challenge_visible(page):
            raise HardStop("The application page shows a captcha.")

    def container(self, page) -> str:
        return self.form_selector

    def submit(self, page) -> None:
        button = page.locator(self.submit_selector).last
        if button.count() == 0:
            raise HardStop("Could not find the Submit button.")
        button.click()

    def for_run(self, tools) -> "Adapter":
        """The adapter one run uses. Stateless adapters are shared; the generic one gets the run's AI tools."""
        return self

    # The engine only calls these (tests swap the adapter, not the browser).
    def read_form(self, page) -> list[dict]:
        fields = forms.extract(page, self.container(page))
        if not fields:
            block = forms.blocked(page)  # e.g. an Apply button that led to a block page
            if block:
                raise Blocked(block)
        return fields

    def needs_human(self, page) -> str | None:
        """Why this form can't be submitted for the user, read before anything is filled."""
        if forms.human_checkbox(page):
            return "This form has an \"I am human\" captcha, so it can't be submitted for you."
        return None

    def fill_form(self, page, fields: list[dict], values: dict[str, str], files: dict[str, str]) -> list[str]:
        return forms.fill(page, fields, values, files)

    def submit_and_wait(self, page, wait_ms: int) -> forms.Outcome:
        if forms.challenge_visible(page):
            return forms.Outcome("captcha", "A captcha is showing on the form.")
        self.submit(page)
        return forms.outcome(page, wait_ms=wait_ms)

    def screenshot(self, page) -> bytes:
        return page.screenshot(full_page=True)

    def page_url(self, page) -> str:
        return page.url


class GreenhouseAdapter(Adapter):
    name = "greenhouse"
    form_selector = "form#application-form, form#application_form, form[action*='greenhouse'], form"
    submit_selector = ('button[type="submit"]:has-text("Submit"), input[type="submit"], '
                       'button:has-text("Submit application"), button[type="submit"]')

    def open(self, page, url: str, timeout_ms: int) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host.endswith("greenhouse.io"):
            # Company career page embedding the Greenhouse form: open the iframe's own page.
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            frame = page.locator('iframe#grnhse_iframe, iframe[src*="greenhouse.io"]').first
            src = frame.get_attribute("src") if frame.count() else None
            if not src:
                if page.locator("form#application-form, form#application_form").count():
                    super().open(page, url, timeout_ms)  # the form is on the company page itself
                    return
                raise HardStop("This company page doesn't show its Greenhouse form in a way we can open.")
            url = src
        super().open(page, url, timeout_ms)
        apply_button = page.locator('button:has-text("Apply"), a:has-text("Apply for this job")').first
        if page.locator("form").count() == 0 and apply_button.count():
            apply_button.click()
            page.wait_for_timeout(1000)

    def container(self, page) -> str:
        for sel in ("form#application-form", "form#application_form"):
            if page.locator(sel).count():
                return sel
        return "form"


class LeverAdapter(Adapter):
    name = "lever"
    form_selector = "form#application-form, form.application-form, form"
    submit_selector = '#btn-submit, button[type="submit"]:has-text("Submit"), button[type="submit"]'

    def open(self, page, url: str, timeout_ms: int) -> None:
        parts = urlparse(url)
        if (parts.hostname or "").endswith("lever.co") and not re.search(r"/apply/?$", parts.path):
            url = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}/apply"
        super().open(page, url, timeout_ms)

    def container(self, page) -> str:
        return "form#application-form" if page.locator("form#application-form").count() else "form"


class LinkedInAdapter(Adapter):
    name = "linkedin"
    can_submit = False  # never: the user submits LinkedIn applications themselves

    def open(self, page, url: str, timeout_ms: int) -> None:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        button = page.get_by_role("button", name=re.compile(r"Easy Apply", re.I))
        if button.count() == 0:
            if forms.login_wall(page) or "login" in page.url or "authwall" in page.url:
                raise HardStop("Sign in to LinkedIn in the browser window that opened, then start Fast apply again.")
            raise HardStop("This LinkedIn job has no Easy Apply button — apply on the company's site.")
        button.first.click()
        page.wait_for_timeout(1500)

    def container(self, page) -> str:
        return 'div[role="dialog"]'

    def submit(self, page) -> None:  # pragma: no cover - guarded by can_submit
        raise HardStop("LinkedIn applications are submitted by you, not by the app.")


from app.apply.generic import GenericAdapter  # noqa: E402 — it subclasses Adapter

ADAPTERS: dict[str, Adapter] = {a.name: a for a in (GreenhouseAdapter(), LeverAdapter(), LinkedInAdapter(),
                                                    GenericAdapter())}


def get(name: str) -> Adapter | None:
    return ADAPTERS.get(name)
