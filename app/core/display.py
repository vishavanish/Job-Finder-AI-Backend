"""
app/core/display.py
---------------------
Detects whether a real display is available. `/apply` launches a visible
Chromium window on purpose (so a human can review/submit Easy Apply
forms) — that only works on a machine with a display attached (your
laptop, a VM with a virtual display, etc). Most production hosting
(containers, headless Linux servers) has none.

If headless=False is requested but no display is available, this fails
LOUDLY with a clear error rather than letting Playwright crash deep in a
background thread with a cryptic X11/display error that's hard to trace
back to "you're running this on a headless server."
"""
from __future__ import annotations

import os
import platform
import shutil


def has_display() -> bool:
    """Best-effort check. Not perfect (a DISPLAY var can be set but stale,
    or a Windows session can be a non-interactive service context), but
    good enough to catch the common "deployed to a headless Linux
    container" case before Playwright does."""
    system = platform.system()

    if system == "Windows":
        # Windows almost always has a display session available unless
        # running as certain background services — treat as available.
        return True

    if system == "Darwin":
        return True

    # Linux: the standard signal is the DISPLAY (X11) or WAYLAND_DISPLAY
    # env var being set.
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_headless(requested_headless: bool) -> tuple[bool, str | None]:
    """Returns (actual_headless_value, warning_message_or_None).

    If the caller asked for headless=False but no display exists, this
    overrides to headless=True and returns a warning the caller should
    surface (via progress_cb / the API response) rather than silently
    swallowing the mismatch.
    """
    if requested_headless:
        return True, None

    if has_display():
        return False, None

    warning = (
        "headless=False was requested but no display was detected on this "
        "server (Linux with no DISPLAY/WAYLAND_DISPLAY set) — forcing "
        "headless=True. Playwright cannot open a visible browser window "
        "here; run /apply from a machine with a display if you need "
        "visual review, or explicitly pass headless=true."
    )
    return True, warning