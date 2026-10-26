"""
app/apply/sites.py
--------------------
Which career sites the AI form reader (P4c, app/apply/generic.py) takes, and
the host helpers it uses to keep the browser on the job's own site. Kept
apart from the adapter so apply_options can import it without Playwright code.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Hosted application forms that need no account. Workday and iCIMS are left out: they ask for one.
GENERIC_HOSTS = {
    "ashbyhq.com": "Ashby", "smartrecruiters.com": "SmartRecruiters", "workable.com": "Workable",
    "recruitee.com": "Recruitee", "personio.de": "Personio", "personio.com": "Personio", "breezy.hr": "Breezy",
    "teamtailor.com": "Teamtailor", "rippling.com": "Rippling", "gem.com": "Gem", "jobvite.com": "Jobvite",
    "applytojob.com": "JazzHR", "pinpointhq.com": "Pinpoint",
}
# Company-ATS jobs keep the platform name as their source (P6): a custom careers host on one of these
# platforms is still a generic form.
GENERIC_SOURCES = {name.lower() for name in GENERIC_HOSTS.values()}


def host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def site_of(host: str) -> str:
    """Registrable-ish domain: the last two labels (three for co.in / co.uk style names)."""
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def generic_host(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in GENERIC_HOSTS)
