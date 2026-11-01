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

# A direct apply link on any other host is the company's own career page, and the AI form reader tries it
# (it stops on a sign-in page anyway) — except job boards, which need the user's own account there, and
# career suites known to ask for an account before the form.
JOB_BOARD_SITES = {
    "indeed.com", "linkedin.com", "naukri.com", "glassdoor.com", "glassdoor.co.in", "foundit.in",
    "monster.com", "monsterindia.com", "shine.com", "timesjobs.com", "instahyre.com", "cutshort.io",
    "wellfound.com", "angel.co", "ziprecruiter.com", "simplyhired.com", "iimjobs.com", "hirist.com",
    "hirist.tech", "apna.co", "internshala.com", "google.com", "bayt.com", "dice.com", "careerbuilder.com",
}
ACCOUNT_SITES = {
    "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "icims.com", "taleo.net", "brassring.com",
    "successfactors.com", "successfactors.eu", "oraclecloud.com", "ultipro.com", "ukg.com", "adp.com",
    "darwinbox.in", "peoplestrong.com", "phenompeople.com", "avature.net",
}


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


def _on(host: str, sites: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in sites)


def career_page(url: str) -> bool:
    """A public https/http page that is not a job board or an account-only career suite."""
    if not (url or "").startswith(("http://", "https://")):
        return False
    host = host_of(url)
    if not host or "." not in host or host.replace(".", "").isdigit() or host == "localhost":
        return False
    return not _on(host, JOB_BOARD_SITES) and not _on(host, ACCOUNT_SITES)
