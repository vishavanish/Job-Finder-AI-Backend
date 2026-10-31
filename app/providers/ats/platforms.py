"""
app/providers/ats/platforms.py
---------------------------------
Public job-board APIs of company ATS platforms, for the ATS Discovery
index (CLAUDE.md 7.6, P6 plan §3.0). One `AtsPlatform` per platform:

  parse_url(url)            careers / job URL -> AtsRef, or None
  list_jobs(ref, etag)      the company's open jobs (conditional request
                            when the platform sends an ETag)
  search(ref, text)         server-side text search, where the API has one
                            (Workday, SmartRecruiters) — used for big
                            tenants and the live top-up
  job_detail(ref, id)       full description of one job, where the list
                            has none

Every job comes back as a plain dict: external_id, title, location,
department, url, posted (raw), remote, country, description (plain text,
may be ""). HTTP goes through job_sources.base.request (tests swap it);
redirects are never followed (a redirect is treated as "not found").

`GoneError` = the slug is not (or no longer) on this platform (404/410 or
a redirect). Other failures raise SourceError. Adding a platform = one
class here, a fixture in tests/fixtures/ats/ and seed rows.

Live-checked 2026-10-19: Greenhouse (ETag/304), Lever, Recruitee, Workday
(searchText), Ashby, SmartRecruiters (country=), Personio, Breezy,
Teamtailor (RSS), Rippling, Gem; 2026-10-31: Keka, Zoho Recruit (India), Oracle Cloud. Not built: BambooHR (careers JSON redirected for
every tenant tried) and Homerun (its public feed returns Homerun's own jobs
for any company name; per-company feeds use unguessable URLs).
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET  # expat >= 2.4 blocks entity-expansion bombs
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import requests

from app.providers.job_sources import base
from app.providers.job_sources.base import SourceError, strip_html


class GoneError(SourceError):
    """The company is not on this platform (any more)."""


@dataclass(frozen=True)
class AtsRef:
    platform: str
    slug: str
    host: str | None = None   # Workday tenant host, or a Teamtailor custom domain
    site: str | None = None   # Workday career site


@dataclass
class ListResult:
    jobs: list[dict] = field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False
    company_name: str | None = None
    complete: bool = True      # False when capped: missing jobs are not closed at once


_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PATH = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PRIVATE = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10")]


def valid_subdomain(slug: str) -> bool:
    return bool(_SUBDOMAIN.match((slug or "").lower()))


def valid_path_slug(slug: str) -> bool:
    return bool(_PATH.match(slug or ""))


def public_host(host: str) -> bool:
    """SSRF guard for hosts we did not hard-code: every address must be public."""
    host = (host or "").lower()
    if not _HOST.match(host):
        return False
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(host, 443)}
    except socket.gaierror:
        return False
    return bool(ips) and not any(ipaddress.ip_address(ip) in net for ip in ips for net in _PRIVATE)


def snippet(text: str, n: int = 300) -> str:
    return strip_html(text or "")[:n]


def _plain(text: Any) -> str:
    return strip_html(str(text or ""))


def _remote_location(location: str, remote: bool) -> str:
    if remote and "remote" not in location.lower():
        return f"{location} (Remote)" if location else "Remote"
    return location


def _job(*, external_id: Any, title: Any, location: str = "", department: Any = "", url: Any = "",
         posted: Any = "", remote: bool = False, country: Any = "", description: Any = "") -> dict:
    location = _plain(location)
    remote = bool(remote) or "remote" in location.lower()
    return {
        "external_id": str(external_id or "").strip()[:128],
        "title": _plain(title)[:500],
        "location": _remote_location(location, remote)[:255],
        "department": _plain(department)[:255],
        "url": str(url or "").strip(),
        "posted": str(posted or ""),
        "remote": remote,
        "country": _plain(country)[:64],
        "description": _plain(description),
    }


def _get(ref: AtsRef, method: str, url: str, *, etag: str | None = None, as_text: bool = False,
         accept: str = "application/json", **kwargs) -> tuple[Any, str | None, bool]:
    """(body, etag, not_modified). 404/410/redirect -> GoneError; other non-200 -> SourceError."""
    headers = {"Accept": accept}
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = base.request(method, url, headers=headers, allow_redirects=False, **kwargs)
    except requests.RequestException as e:
        raise SourceError(f"{ref.platform}: {type(e).__name__}") from e
    if resp.status_code == 304:
        return None, etag, True
    if resp.status_code in (404, 410) or 300 <= resp.status_code < 400:
        raise GoneError(f"{ref.platform} '{ref.slug}': HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise SourceError(f"{ref.platform} '{ref.slug}': HTTP {resp.status_code}")
    new_etag = resp.headers.get("ETag") if hasattr(resp, "headers") and resp.headers else None
    if as_text:
        return resp.text, new_etag, False
    try:
        return resp.json(), new_etag, False
    except ValueError as e:
        raise SourceError(f"{ref.platform} '{ref.slug}': response was not JSON") from e


def _xml(ref: AtsRef, text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError(f"{ref.platform} '{ref.slug}': feed was not valid XML") from e


_TITLE = re.compile(r"<title[^>]*>([^<]{1,200})</title>", re.I)
_OG_TITLE = re.compile(r"""<meta[^>]+property=["']og:(?:site_name|title)["'][^>]+content=["']([^"']{1,200})""", re.I)
_NAME_NOISE = re.compile(
    r"^\s*(careers?|jobs?|open positions|work)\s+(at|@|with)\s+"
    r"|^\s*(careers?|jobs?|current openings)\s*[-|–—:]\s*"
    r"|\s*[-|–—:]\s*(current openings|careers?|jobs?|job board|open (positions|roles)).*$"
    r"|\s+(careers?|jobs?|job board)\s*$", re.I)
_GENERIC_NAMES = {"", "careers", "career", "jobs", "job board", "current openings", "open positions", "home"}


def clean_board_name(text: str | None) -> str | None:
    """A careers page title -> the company name ("Ramp Jobs" -> "Ramp", "CAREERS AT NVIDIA" -> "NVIDIA",
    "ThoughtWorks - Current Openings" -> "ThoughtWorks"); None when only generic words are left."""
    name = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    for _ in range(2):
        name = _NAME_NOISE.sub("", name).strip(" -|")
    if name.lower() in _GENERIC_NAMES or len(name) > 120:
        return None
    return name


def page_title_name(html_text: str) -> str | None:
    """The company name from a careers page: <title> first, og:title / og:site_name when that is empty."""
    for pattern in (_TITLE, _OG_TITLE):
        for m in pattern.finditer(html_text[:200_000]):
            name = clean_board_name(m.group(1))
            if name:
                return name
    return None


class AtsPlatform:
    key: str = ""
    name: str = ""
    host_suffix: str = ""          # hostname suffix that identifies this platform in a URL
    has_search: bool = False
    has_detail: bool = False
    name_from_page: bool = False   # the list API has no company name; the careers page title has it (QA3 §D)

    def parse_url(self, url: str) -> AtsRef | None:
        raise NotImplementedError

    def careers_url(self, ref: AtsRef) -> str:
        raise NotImplementedError

    def list_jobs(self, ref: AtsRef, *, etag: str | None = None, max_jobs: int = 1000,
                  markets: tuple[str, ...] = ()) -> ListResult:
        raise NotImplementedError

    def search(self, ref: AtsRef, text: str, *, limit: int = 100) -> list[dict]:
        return []

    def job_detail(self, ref: AtsRef, external_id: str) -> str:
        return ""

    def board_name(self, ref: AtsRef, jobs: list[dict]) -> str | None:
        """The company's own name when the job list has none (one extra request; None when not found)."""
        if not self.name_from_page:
            return None
        text, _, _ = _get(ref, "GET", self.careers_url(ref), as_text=True, accept="text/html")
        return page_title_name(text or "")

    def valid(self, ref: AtsRef) -> bool:
        return valid_subdomain(ref.slug)


def _parts(url: str) -> tuple[str, list[str]]:
    p = urlparse(url if "://" in url else f"https://{url}")
    return (p.hostname or "").lower(), [s for s in p.path.split("/") if s]


# ---------------------------------------------------------------- Greenhouse

class Greenhouse(AtsPlatform):
    key, name, has_detail = "greenhouse", "Greenhouse", True
    _HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io",
              "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io")

    def parse_url(self, url):
        host, parts = _parts(url)
        if host not in self._HOSTS or not parts:
            return None
        if host == "boards-api.greenhouse.io":
            parts = parts[2:] if parts[:2] == ["v1", "boards"] else []
        if parts and parts[0] == "embed" :
            q = urlparse(url).query
            m = re.search(r"(?:^|&)for=([^&]+)", q)
            return AtsRef(self.key, m.group(1).lower()) if m and valid_path_slug(m.group(1)) else None
        return AtsRef(self.key, parts[0].lower()) if parts and valid_path_slug(parts[0]) else None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://job-boards.greenhouse.io/{ref.slug}"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, new_etag, nm = _get(ref, "GET", f"https://boards-api.greenhouse.io/v1/boards/{quote(ref.slug)}/jobs",
                                  etag=etag)
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs, company = [], None
        for item in (data or {}).get("jobs", []):
            company = company or item.get("company_name")
            meta = {m.get("name", "").lower(): m.get("value") for m in item.get("metadata") or []}
            workplace = str(meta.get("workplace type") or meta.get("location type") or "").lower()
            jobs.append(_job(external_id=item.get("id"), title=item.get("title"),
                             location=(item.get("location") or {}).get("name", ""),
                             url=item.get("absolute_url"), posted=item.get("first_published") or item.get("updated_at"),
                             remote="remote" in workplace))
        return ListResult(jobs=jobs, etag=new_etag, company_name=company)

    def job_detail(self, ref, external_id):
        data, _, _ = _get(ref, "GET",
                          f"https://boards-api.greenhouse.io/v1/boards/{quote(ref.slug)}/jobs/{quote(external_id)}")
        return _plain(strip_html((data or {}).get("content", "")))


# ---------------------------------------------------------------- Lever

class Lever(AtsPlatform):
    key, name, has_detail, name_from_page = "lever", "Lever", True, True

    def parse_url(self, url):
        host, parts = _parts(url)
        if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts and valid_path_slug(parts[0]):
            return AtsRef(self.key, parts[0].lower())
        if host in ("api.lever.co", "api.eu.lever.co") and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
            return AtsRef(self.key, parts[2].lower()) if valid_path_slug(parts[2]) else None
        return None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://jobs.lever.co/{ref.slug}"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, new_etag, nm = _get(ref, "GET", f"https://api.lever.co/v0/postings/{quote(ref.slug)}?mode=json",
                                  etag=etag)
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs = []
        for item in data if isinstance(data, list) else []:
            cat = item.get("categories") or {}
            jobs.append(_job(external_id=item.get("id"), title=item.get("text"), location=cat.get("location", ""),
                             department=cat.get("department") or cat.get("team"), url=item.get("hostedUrl"),
                             posted=item.get("createdAt"), remote=item.get("workplaceType") == "remote",
                             country=item.get("country"), description=item.get("descriptionPlain")))
        return ListResult(jobs=jobs, etag=new_etag)

    def job_detail(self, ref, external_id):
        data, _, _ = _get(ref, "GET", f"https://api.lever.co/v0/postings/{quote(ref.slug)}/{quote(external_id)}")
        return _plain((data or {}).get("descriptionPlain") or (data or {}).get("description", ""))


# ---------------------------------------------------------------- Recruitee

class Recruitee(AtsPlatform):
    key, name, host_suffix = "recruitee", "Recruitee", ".recruitee.com"

    def parse_url(self, url):
        host, _ = _parts(url)
        sub = host[: -len(self.host_suffix)] if host.endswith(self.host_suffix) else ""
        return AtsRef(self.key, sub) if sub and valid_subdomain(sub) else None

    def careers_url(self, ref):
        return f"https://{ref.slug}.recruitee.com/"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, new_etag, nm = _get(ref, "GET", f"https://{ref.slug}.recruitee.com/api/offers/", etag=etag)
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs, company = [], None
        for item in (data or {}).get("offers", []):
            company = company or item.get("company_name")
            location = item.get("location") or ", ".join(filter(None, [item.get("city"), item.get("country")]))
            jobs.append(_job(external_id=item.get("id"), title=item.get("title"), location=location,
                             department=item.get("department"), url=item.get("careers_url"),
                             posted=item.get("published_at"), remote=bool(item.get("remote")),
                             country=item.get("country"),
                             description=f"{item.get('description', '')} {item.get('requirements', '')}"))
        return ListResult(jobs=jobs, etag=new_etag, company_name=company)


# ---------------------------------------------------------------- Ashby

class Ashby(AtsPlatform):
    key, name, name_from_page = "ashby", "Ashby", True

    def parse_url(self, url):
        host, parts = _parts(url)
        if host == "jobs.ashbyhq.com" and parts and valid_path_slug(parts[0]):
            return AtsRef(self.key, parts[0])
        if host == "api.ashbyhq.com" and len(parts) >= 3 and parts[:2] == ["posting-api", "job-board"]:
            return AtsRef(self.key, parts[2]) if valid_path_slug(parts[2]) else None
        return None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://jobs.ashbyhq.com/{ref.slug}"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, new_etag, nm = _get(ref, "GET", f"https://api.ashbyhq.com/posting-api/job-board/{quote(ref.slug)}",
                                  etag=etag)
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs = []
        for item in (data or {}).get("jobs", []):
            if item.get("isListed") is False:
                continue
            jobs.append(_job(external_id=item.get("id") or item.get("jobUrl"), title=item.get("title"),
                             location=item.get("location", ""), department=item.get("department"),
                             url=item.get("jobUrl"), posted=item.get("publishedAt"),
                             remote=bool(item.get("isRemote")),
                             description=item.get("descriptionPlain") or item.get("descriptionHtml")))
        return ListResult(jobs=jobs, etag=new_etag)


# ---------------------------------------------------------------- SmartRecruiters

class SmartRecruiters(AtsPlatform):
    key, name, has_search, has_detail = "smartrecruiters", "SmartRecruiters", True, True
    _COUNTRY = {"india": "in"}

    def parse_url(self, url):
        host, parts = _parts(url)
        if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts \
                and valid_path_slug(parts[0]):
            return AtsRef(self.key, parts[0])
        if host == "api.smartrecruiters.com" and len(parts) >= 3 and parts[:2] == ["v1", "companies"]:
            return AtsRef(self.key, parts[2]) if valid_path_slug(parts[2]) else None
        return None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://careers.smartrecruiters.com/{ref.slug}"

    def _page(self, ref, params: str) -> tuple[list[dict], int, str | None]:
        data, _, _ = _get(ref, "GET", f"https://api.smartrecruiters.com/v1/companies/{quote(ref.slug)}/postings?{params}")
        jobs, company = [], None
        for item in (data or {}).get("content", []):
            company = company or (item.get("company") or {}).get("name")
            loc = item.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
            location = re.sub(r"(,\s*)+", ", ", location).strip(", ")
            jobs.append(_job(external_id=item.get("id"), title=item.get("name"), location=location,
                             department=(item.get("department") or {}).get("label")
                             or (item.get("function") or {}).get("label"),
                             url=f"https://jobs.smartrecruiters.com/{ref.slug}/{item.get('id')}",
                             posted=item.get("releasedDate"), remote=bool(loc.get("remote")),
                             country=loc.get("country")))
        return jobs, int((data or {}).get("totalFound") or 0), company

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        jobs, total, company = self._page(ref, "limit=100&offset=0")
        complete = total <= max_jobs
        if not complete and markets:
            # Big tenant (Bosch: 5,000+): index only the target markets; the rest is searched live.
            jobs = []
            for market in markets:
                code = self._COUNTRY.get(market.lower())
                if code:
                    jobs += self._collect(ref, f"country={code}", max_jobs - len(jobs))
        else:
            jobs += self._collect(ref, "", max_jobs - len(jobs), start=100) if total > 100 else []
        return ListResult(jobs=jobs[:max_jobs], company_name=company, complete=complete)

    def _collect(self, ref, extra: str, budget: int, start: int = 0) -> list[dict]:
        out, offset = [], start
        while budget > 0:
            page, total, _ = self._page(ref, f"limit=100&offset={offset}" + (f"&{extra}" if extra else ""))
            out += page[:budget]
            budget -= len(page)
            offset += 100
            if not page or offset >= total:
                break
        return out

    def search(self, ref, text, *, limit=100):
        jobs, _, _ = self._page(ref, f"limit={min(limit, 100)}&q={quote(text)}")
        return jobs

    def job_detail(self, ref, external_id):
        data, _, _ = _get(ref, "GET",
                          f"https://api.smartrecruiters.com/v1/companies/{quote(ref.slug)}/postings/{quote(external_id)}")
        sections = ((data or {}).get("jobAd") or {}).get("sections") or {}
        return _plain(" ".join((sections.get(k) or {}).get("text", "")
                               for k in ("jobDescription", "qualifications", "additionalInformation")))


# ---------------------------------------------------------------- Workday

class Workday(AtsPlatform):
    key, name, host_suffix, has_search, has_detail = "workday", "Workday", ".myworkdayjobs.com", True, True
    name_from_page = True
    _PAGE = 20

    def parse_url(self, url):
        host, parts = _parts(url)
        if not host.endswith(self.host_suffix) or not _HOST.match(host):
            return None
        tenant = host.split(".")[0]
        if parts and parts[0] == "wday" and len(parts) >= 4:   # /wday/cxs/<tenant>/<site>/...
            site = parts[3]
        else:
            parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", p)]  # drop /en-US/
            site = parts[0] if parts else ""
        if not (valid_subdomain(tenant) and valid_path_slug(site)):
            return None
        return AtsRef(self.key, tenant, host=host, site=site)

    def valid(self, ref):
        return bool(ref.host and ref.host.endswith(self.host_suffix) and ref.site and valid_path_slug(ref.site))

    def careers_url(self, ref):
        return f"https://{ref.host}/{ref.site}"

    def _api(self, ref) -> str:
        return f"https://{ref.host}/wday/cxs/{ref.slug}/{ref.site}"

    def _page(self, ref, text: str, offset: int) -> tuple[list[dict], int]:
        data, _, _ = _get(ref, "POST", f"{self._api(ref)}/jobs",
                          json={"appliedFacets": {}, "limit": self._PAGE, "offset": offset, "searchText": text})
        jobs = []
        for item in (data or {}).get("jobPostings", []):
            path = item.get("externalPath") or ""
            bullets = item.get("bulletFields") or []
            location = item.get("locationsText") or ""
            if re.fullmatch(r"\d+ Locations", location):
                location = ""
            jobs.append(_job(external_id=bullets[0] if bullets else path, title=item.get("title"),
                             location=location, url=f"https://{ref.host}/{ref.site}{path}" if path else "",
                             posted=_workday_posted(item.get("postedOn", ""))) | {"_path": path})
        return jobs, int((data or {}).get("total") or 0)

    def _collect(self, ref, text: str, budget: int) -> tuple[list[dict], int]:
        out, offset, total = [], 0, 0
        while budget > 0:
            page, t = self._page(ref, text, offset)
            total = total or t
            out += page[:budget]
            budget -= len(page)
            offset += self._PAGE
            if not page or offset >= total:
                break
        return out, total

    def list_jobs(self, ref, *, etag=None, max_jobs=400, markets=()):
        jobs, total = self._collect(ref, "", max_jobs)
        if total > max_jobs and markets:
            # Big tenant (NVIDIA: 2,000): the target markets first, then what fits of the rest.
            market_jobs = []
            for market in markets:
                found, _ = self._collect(ref, market, max_jobs - len(market_jobs))
                market_jobs += found
            seen = {j["external_id"] for j in market_jobs}
            jobs = (market_jobs + [j for j in jobs if j["external_id"] not in seen])[:max_jobs]
        return ListResult(jobs=jobs, complete=total <= max_jobs)

    def search(self, ref, text, *, limit=100):
        return self._collect(ref, text, limit)[0]

    def job_detail(self, ref, external_id):
        # external_id is the requisition id; the job path is stored in the URL.
        return ""

    def job_detail_by_url(self, ref, url: str) -> str:
        path = urlparse(url).path
        prefix = f"/{ref.site}"
        if not path.startswith(prefix):
            return ""
        data, _, _ = _get(ref, "GET", f"{self._api(ref)}{path[len(prefix):]}")
        return _plain(((data or {}).get("jobPostingInfo") or {}).get("jobDescription", ""))


# ---------------------------------------------------------------- Oracle Recruiting Cloud (Fusion HCM)

class OracleCloud(AtsPlatform):
    """{host}.oraclecloud.com Candidate Experience sites (Kotak, Tata Capital, Honeywell, Oracle…). The public
    REST resource answers without a session. Big sites (thousands of jobs) are read target markets first
    through the LOCATIONS facet, like Workday; `keyword=` searches titles. `host` = the oraclecloud host,
    `site` = the site number (CX_1, CX_1001…). Live-checked 2026-10-31 on 9 Indian employers' sites."""
    key, name, host_suffix, has_search, has_detail = "oracle_cloud", "Oracle Cloud", ".oraclecloud.com", True, True
    _PAGE = 25
    _SITE = re.compile(r"^CX_\d{1,6}$|^[A-Za-z0-9_]{2,40}$")

    def parse_url(self, url):
        host, parts = _parts(url)
        if not host.endswith(self.host_suffix) or not _HOST.match(host):
            return None
        site = None
        if "sites" in parts and parts.index("sites") + 1 < len(parts):        # /hcmUI/CandidateExperience/en/sites/CX_1/…
            site = parts[parts.index("sites") + 1]
        else:                                                                 # the REST API: …finder=findReqs;siteNumber=CX_1
            m = re.search(r"siteNumber=([A-Za-z0-9_]+)", url)
            site = m.group(1) if m else None
        tenant = host.split(".")[0]
        if not site or not self._SITE.match(site) or not valid_subdomain(tenant):
            return None
        return AtsRef(self.key, tenant, host=host, site=site)

    def valid(self, ref):
        return bool(ref.host and ref.host.endswith(self.host_suffix) and _HOST.match(ref.host)
                    and ref.site and self._SITE.match(ref.site))

    def careers_url(self, ref):
        return f"https://{ref.host}/hcmUI/CandidateExperience/en/sites/{ref.site}/jobs"

    def _api(self, ref) -> str:
        return f"https://{ref.host}/hcmRestApi/resources/latest"

    def _page(self, ref, offset: int, *, keyword: str = "", facet: str = "",
              with_facets: bool = False) -> tuple[list[dict], int, list[dict]]:
        finder = f"siteNumber={ref.site},limit={self._PAGE},offset={offset},sortBy=POSTING_DATES_DESC"
        if keyword:
            finder += f",keyword={quote(keyword)}"
        if facet:
            finder += f",selectedLocationsFacet={facet}"
        if with_facets:
            finder += ",facetsList=LOCATIONS"
        data, _, _ = _get(ref, "GET", f"{self._api(ref)}/recruitingCEJobRequisitions?onlyData=true"
                                      f"&expand=requisitionList.secondaryLocations,locationsFacet&finder=findReqs;{finder}")
        block = ((data or {}).get("items") or [{}])[0]
        jobs = []
        for item in block.get("requisitionList") or []:
            places = [item.get("PrimaryLocation") or ""] + [
                loc.get("Name") or "" for loc in item.get("secondaryLocations") or [] if isinstance(loc, dict)]
            workplace = str(item.get("WorkplaceType") or item.get("WorkplaceTypeCode") or "")
            jobs.append(_job(external_id=item.get("Id"), title=item.get("Title"),
                             location="; ".join(dict.fromkeys(p for p in places if p)),
                             department=item.get("Department") or item.get("JobFamily"),
                             url=f"https://{ref.host}/hcmUI/CandidateExperience/en/sites/{ref.site}/job/{item.get('Id')}",
                             posted=item.get("PostedDate"), remote="remote" in workplace.lower(),
                             country=item.get("PrimaryLocationCountry"),
                             description=" ".join(filter(None, (item.get("ShortDescriptionStr"),
                                                                item.get("ExternalResponsibilitiesStr"),
                                                                item.get("ExternalQualificationsStr")))))
                        | {"_employer": item.get("LegalEmployer") or ""})
        return jobs, int(block.get("TotalJobsCount") or 0), block.get("locationsFacet") or []

    def _collect(self, ref, budget: int, **kw) -> tuple[list[dict], int, list[dict]]:
        out, offset, total, facets = [], 0, 0, []
        while budget > 0:
            page, t, f = self._page(ref, offset, with_facets=offset == 0, **kw)
            total, facets = total or t, facets or f
            out += page[:budget]
            budget -= len(page)
            offset += self._PAGE
            if not page or offset >= total:
                break
        return out, total, facets

    def list_jobs(self, ref, *, etag=None, max_jobs=400, markets=()):
        jobs, total, facets = self._collect(ref, max_jobs)
        if total > max_jobs and markets:
            # Big site (Kotak: 12,000): the target markets' location facets first, then what fits of the rest.
            wanted = {m.strip().lower() for m in markets}
            market_jobs: list[dict] = []
            for facet in facets:
                if (facet.get("Name") or "").strip().lower() in wanted and len(market_jobs) < max_jobs:
                    found, _, _ = self._collect(ref, max_jobs - len(market_jobs), facet=str(facet.get("Id")))
                    market_jobs += found
            seen = {j["external_id"] for j in market_jobs}
            jobs = (market_jobs + [j for j in jobs if j["external_id"] not in seen])[:max_jobs]
        return ListResult(jobs=jobs, complete=total <= max_jobs)

    def search(self, ref, text, *, limit=100):
        return self._collect(ref, limit, keyword=text)[0]

    def job_detail(self, ref, external_id):
        data, _, _ = _get(ref, "GET", f'{self._api(ref)}/recruitingCEJobRequisitionDetails?onlyData=true'
                                      f'&expand=all&finder=ById;Id="{quote(str(external_id))}",siteNumber={ref.site}')
        item = ((data or {}).get("items") or [{}])[0]
        return _plain(" ".join(filter(None, (item.get("ExternalDescriptionStr"), item.get("ExternalResponsibilitiesStr"),
                                             item.get("ExternalQualificationsStr")))))

    def board_name(self, ref, jobs):
        # The careers page title ("Honeywell"); some sites redirect it, then the jobs' legal employer.
        try:
            text, _, _ = _get(ref, "GET", self.careers_url(ref), as_text=True, accept="text/html")
            name = page_title_name(text or "")
        except SourceError:
            name = None
        if not name:
            employers = [j.get("_employer") for j in jobs if j.get("_employer")]
            name = clean_board_name(max(set(employers), key=employers.count)) if employers else None
        return name


def _workday_posted(text: str) -> str:
    t = (text or "").lower().replace("posted", "").strip()
    if t == "today":
        return "today"
    if t == "yesterday":
        return "1 day ago"
    return t


# ---------------------------------------------------------------- Workable

class Workable(AtsPlatform):
    key, name, name_from_page = "workable", "Workable", True

    def parse_url(self, url):
        host, parts = _parts(url)
        if host == "apply.workable.com" and parts and valid_path_slug(parts[0]) and parts[0] not in ("api", "j"):
            return AtsRef(self.key, parts[0].lower())
        sub = host[: -len(".workable.com")] if host.endswith(".workable.com") else ""
        if sub and sub not in ("apply", "www") and valid_subdomain(sub):
            return AtsRef(self.key, sub)
        return None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://apply.workable.com/{ref.slug}/"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        jobs, token = [], None
        for _ in range(20):
            body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
            if token:
                body["token"] = token
            data, _, _ = _get(ref, "POST", f"https://apply.workable.com/api/v3/accounts/{quote(ref.slug)}/jobs",
                              json=body)
            for item in (data or {}).get("results", []):
                loc = item.get("location") or {}
                jobs.append(_job(external_id=item.get("shortcode") or item.get("id"), title=item.get("title"),
                                 location=", ".join(filter(None, [loc.get("city"), loc.get("region"),
                                                                  loc.get("country")])),
                                 department=", ".join(item.get("department") or []) if isinstance(
                                     item.get("department"), list) else item.get("department"),
                                 url=f"https://apply.workable.com/{ref.slug}/j/{item.get('shortcode', '')}/",
                                 posted=item.get("published"), remote=bool(item.get("remote")),
                                 country=loc.get("country")))
            token = (data or {}).get("nextPage")
            if not token or len(jobs) >= max_jobs:
                break
        return ListResult(jobs=jobs[:max_jobs], complete=not token)


# ---------------------------------------------------------------- Personio

class Personio(AtsPlatform):
    key, name = "personio", "Personio"

    def parse_url(self, url):
        host, _ = _parts(url)
        m = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(de|com)", host)
        return AtsRef(self.key, m.group(1)) if m and valid_subdomain(m.group(1)) else None

    def careers_url(self, ref):
        return f"https://{ref.slug}.jobs.personio.de/"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        text, new_etag, nm = _get(ref, "GET", f"https://{ref.slug}.jobs.personio.de/xml", etag=etag, as_text=True,
                                  accept="application/xml")
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs, company = [], None
        for pos in _xml(ref, text).iterfind("position"):
            company = company or pos.findtext("subcompany")
            job_id = (pos.findtext("id") or "").strip()
            parts = [f"{d.findtext('name') or ''}: {d.findtext('value') or ''}"
                     for d in pos.iterfind("jobDescriptions/jobDescription")]
            jobs.append(_job(external_id=job_id, title=pos.findtext("name"), location=pos.findtext("office") or "",
                             department=pos.findtext("department"),
                             url=f"https://{ref.slug}.jobs.personio.de/job/{job_id}",
                             posted=pos.findtext("createdAt"), description=" ".join(parts)))
        return ListResult(jobs=jobs, etag=new_etag, company_name=company)


# ---------------------------------------------------------------- Breezy

class Breezy(AtsPlatform):
    key, name, host_suffix = "breezy", "Breezy", ".breezy.hr"

    def parse_url(self, url):
        host, _ = _parts(url)
        sub = host[: -len(self.host_suffix)] if host.endswith(self.host_suffix) else ""
        return AtsRef(self.key, sub) if sub and sub != "app" and valid_subdomain(sub) else None

    def careers_url(self, ref):
        return f"https://{ref.slug}.breezy.hr/"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, new_etag, nm = _get(ref, "GET", f"https://{ref.slug}.breezy.hr/json", etag=etag)
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs, company = [], None
        for item in data if isinstance(data, list) else []:
            company = company or (item.get("company") or {}).get("name")
            loc = item.get("location") or {}
            country = (loc.get("country") or {}).get("name")
            jobs.append(_job(external_id=item.get("id"), title=item.get("name"),
                             location=loc.get("name") or ", ".join(filter(None, [loc.get("city"), country])),
                             department=item.get("department"), url=item.get("url"),
                             posted=item.get("published_date"), remote=bool(loc.get("is_remote")), country=country))
        return ListResult(jobs=jobs, etag=new_etag, company_name=company)


# ---------------------------------------------------------------- Teamtailor

_TT = "{https://teamtailor.com/locations}"


class Teamtailor(AtsPlatform):
    key, name, host_suffix = "teamtailor", "Teamtailor", ".teamtailor.com"

    def parse_url(self, url):
        host, _ = _parts(url)
        sub = host[: -len(self.host_suffix)] if host.endswith(self.host_suffix) else ""
        if sub and sub not in ("www", "app", "career") and valid_subdomain(sub):
            return AtsRef(self.key, sub)
        return None

    def ref_for_custom_host(self, host: str) -> AtsRef:
        """Companies on a custom careers domain (careers.acme.com) — added by a user or admin, never guessed."""
        return AtsRef(self.key, host.split(".")[0] if host != "career.teamtailor.com" else "teamtailor", host=host)

    def valid(self, ref):
        return valid_subdomain(ref.slug) and (ref.host is None or bool(_HOST.match(ref.host)))

    def _host(self, ref) -> str:
        return ref.host or f"{ref.slug}.teamtailor.com"

    def careers_url(self, ref):
        return f"https://{self._host(ref)}/jobs"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        if ref.host and not ref.host.endswith(self.host_suffix) and not public_host(ref.host):
            raise SourceError(f"teamtailor '{ref.slug}': host is not public")
        text, new_etag, nm = _get(ref, "GET", f"https://{self._host(ref)}/jobs.rss", etag=etag, as_text=True,
                                  accept="application/rss+xml")
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        root = _xml(ref, text)
        jobs = []
        for item in root.iterfind("channel/item"):
            locs = item.findall(f"{_TT}locations/{_TT}location")
            names = [loc.findtext(f"{_TT}city") or loc.findtext(f"{_TT}name") or "" for loc in locs]
            country = locs[0].findtext(f"{_TT}country") if locs else ""
            location = "; ".join(n for n in names if n)
            if country and location and country not in location:
                location = f"{location}, {country}"
            jobs.append(_job(external_id=item.findtext("guid") or item.findtext("link"), title=item.findtext("title"),
                             location=location, department=item.findtext(f"{_TT}department"),
                             url=item.findtext("link"), posted=item.findtext("pubDate"),
                             remote=(item.findtext("remoteStatus") or "").lower() in ("fully", "remote"),
                             country=country, description=item.findtext("description")))
        return ListResult(jobs=jobs, etag=new_etag, company_name=root.findtext("channel/title"))


# ---------------------------------------------------------------- Rippling

class Rippling(AtsPlatform):
    key, name, has_detail = "rippling", "Rippling", True

    def parse_url(self, url):
        host, parts = _parts(url)
        if host != "ats.rippling.com" or not parts:
            return None
        if parts[:3] == ["api", "v2", "board"] and len(parts) >= 4:
            parts = parts[3:]
        slug = parts[0]
        return AtsRef(self.key, slug) if valid_path_slug(slug) and slug != "api" else None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://ats.rippling.com/{ref.slug}/jobs"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        jobs, page, pages = [], 0, 1
        while page < pages and len(jobs) < max_jobs and page < 60:
            data, _, _ = _get(ref, "GET", f"https://ats.rippling.com/api/v2/board/{quote(ref.slug)}/jobs?page={page}")
            pages = int((data or {}).get("totalPages") or 0)
            for item in (data or {}).get("items", []):
                locs = item.get("locations") or []
                first = locs[0] if locs else {}
                location = "; ".join(filter(None, (loc.get("name") for loc in locs)))
                jobs.append(_job(external_id=item.get("id"), title=item.get("name"), location=location,
                                 department=(item.get("department") or {}).get("name"), url=item.get("url"),
                                 remote=any(loc.get("workplaceType") == "REMOTE" for loc in locs),
                                 country=first.get("country")))
            page += 1
        return ListResult(jobs=jobs[:max_jobs], complete=page >= pages)

    def job_detail(self, ref, external_id):
        data, _, _ = _get(ref, "GET",
                          f"https://ats.rippling.com/api/v2/board/{quote(ref.slug)}/jobs/{quote(external_id)}")
        desc = (data or {}).get("description") or {}
        return _plain(desc.get("role", "") if isinstance(desc, dict) else desc)

    def board_name(self, ref, jobs):
        # The list has no company name; each job's detail carries `companyName`.
        if not jobs:
            return None
        data, _, _ = _get(ref, "GET", f"https://ats.rippling.com/api/v2/board/{quote(ref.slug)}/jobs/"
                                      f"{quote(str(jobs[0]['external_id']))}")
        return clean_board_name((data or {}).get("companyName"))


# ---------------------------------------------------------------- Gem

class Gem(AtsPlatform):
    key, name, name_from_page = "gem", "Gem", True

    def parse_url(self, url):
        host, parts = _parts(url)
        if host == "jobs.gem.com" and parts and valid_path_slug(parts[0]):
            return AtsRef(self.key, parts[0])
        if host == "api.gem.com" and len(parts) >= 4 and parts[:3] == ["job_board", "v0", parts[2]]:
            return AtsRef(self.key, parts[2]) if valid_path_slug(parts[2]) else None
        return None

    def valid(self, ref):
        return valid_path_slug(ref.slug)

    def careers_url(self, ref):
        return f"https://jobs.gem.com/{ref.slug}"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, new_etag, nm = _get(ref, "GET", f"https://api.gem.com/job_board/v0/{quote(ref.slug)}/job_posts/",
                                  etag=etag)
        if nm:
            return ListResult(etag=new_etag, not_modified=True)
        jobs = []
        for item in data if isinstance(data, list) else []:
            loc = item.get("location") or {}
            depts = item.get("departments") or []
            jobs.append(_job(external_id=item.get("id"), title=item.get("title"),
                             location=loc.get("name", "") if isinstance(loc, dict) else str(loc),
                             department=depts[0].get("name") if depts and isinstance(depts[0], dict) else "",
                             url=item.get("absolute_url"),
                             posted=item.get("first_published_at") or item.get("created_at"),
                             remote=str(item.get("location_type") or "").lower() == "remote",
                             description=item.get("content_plain") or item.get("content")))
        return ListResult(jobs=jobs, etag=new_etag)


# ---------------------------------------------------------------- Keka (India)

class Keka(AtsPlatform):
    """{co}.keka.com/careers: the portal's own JSON list (full description, cities with country codes,
    published date). Unknown companies redirect to a "tenant not found" page (GoneError)."""
    key, name, host_suffix = "keka", "Keka", ".keka.com"
    _RESERVED = {"www", "app", "help", "support", "cdn", "developers", "status", "blog"}

    def parse_url(self, url):
        host, parts = _parts(url)
        if not host.endswith(self.host_suffix) or not parts or parts[0].lower() != "careers":
            return None
        sub = host[: -len(self.host_suffix)]
        return AtsRef(self.key, sub) if valid_subdomain(sub) and sub not in self._RESERVED else None

    def careers_url(self, ref):
        return f"https://{ref.slug}.keka.com/careers/"

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        data, _, _ = _get(ref, "GET", f"https://{ref.slug}.keka.com/careers/api/jobs/default/active")
        jobs = []
        for item in data if isinstance(data, list) else []:
            locs = [loc for loc in item.get("jobLocations") or [] if isinstance(loc, dict)]
            location = "; ".join(filter(None, (loc.get("name") or loc.get("city") for loc in locs)))
            jobs.append(_job(external_id=item.get("id"), title=item.get("title"), location=location,
                             department=item.get("departmentName"),
                             url=f"https://{ref.slug}.keka.com/careers/jobdetails/{item.get('id')}",
                             posted=item.get("publishedOn"),
                             country=locs[0].get("countryName") if locs else "",
                             description=item.get("description")))
        return ListResult(jobs=jobs[:max_jobs], complete=len(jobs) <= max_jobs)

    def board_name(self, ref, jobs):
        data, _, _ = _get(ref, "GET", f"https://{ref.slug}.keka.com/careers/api/organization/default/careerportalinfo")
        return clean_board_name((data or {}).get("name"))


# ---------------------------------------------------------------- Zoho Recruit (India and elsewhere)

class ZohoRecruit(AtsPlatform):
    """{co}.zohorecruit.{com,in,eu,com.au}/jobs/Careers: no JSON API, but the public page embeds the job list
    as JSON in a hidden input (HTML-escaped). The page is large (~1-2 MB): crawl it on the normal schedule
    only. `host` holds the regional domain. Unknown companies get Zoho's error page (GoneError)."""
    key, name = "zoho_recruit", "Zoho Recruit"
    DOMAINS = ("zohorecruit.com", "zohorecruit.in", "zohorecruit.eu", "zohorecruit.com.au")
    _MARKER = re.compile(r'"Job_Opening_Name"\s*:\s*"')

    def parse_url(self, url):
        host, parts = _parts(url)
        for domain in self.DOMAINS:
            if host.endswith("." + domain):
                sub = host[: -len(domain) - 1]
                if valid_subdomain(sub) and sub != "www" and parts[:1] in (["jobs"], ["recruit"], []):
                    return AtsRef(self.key, sub, host=domain)
        return None

    def valid(self, ref):
        return valid_subdomain(ref.slug) and ref.host in self.DOMAINS

    def careers_url(self, ref):
        return f"https://{ref.slug}.{ref.host}/jobs/Careers"

    @classmethod
    def _embedded_jobs(cls, page: str) -> list[dict] | None:
        text = html.unescape(page or "")
        found = cls._MARKER.search(text)
        if found is None:
            return None
        start = text.rfind("[", 0, found.start())
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
        return [j for j in data if isinstance(j, dict)] if isinstance(data, list) else None

    _SITE_NAME = re.compile(r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']{1,200})""", re.I)

    @classmethod
    def _company(cls, page: str) -> str | None:
        # og:site_name is the company ("Kanerika Software"); the <title> may be the Zoho account's name
        # ("Jobs at PeoplePlus"), so it is only the fallback.
        found = cls._SITE_NAME.search(page[:200_000])
        return (clean_board_name(found.group(1)) if found else None) or page_title_name(page)

    @staticmethod
    def _location(item: dict) -> str:
        parts: list[str] = []
        for value in (item.get("City"), item.get("State"), item.get("Country")):
            for piece in str(value or "").split(","):
                piece = piece.strip()
                if piece and piece.lower() not in (p.lower() for p in parts):
                    parts.append(piece.title() if piece.islower() else piece)
        return ", ".join(parts)

    def list_jobs(self, ref, *, etag=None, max_jobs=1000, markets=()):
        page, _, _ = _get(ref, "GET", self.careers_url(ref), as_text=True, accept="text/html")
        items = self._embedded_jobs(page)
        if items is None:
            if "cl-error-block" in (page or ""):
                raise GoneError(f"{self.key} '{ref.slug}': not a Zoho Recruit careers site")
            items = []  # a careers page with no openings right now
        jobs = []
        for item in items:
            if item.get("Publish") is False:
                continue
            title = item.get("Posting_Title") or item.get("Job_Opening_Name")
            words = re.sub(r"[^A-Za-z0-9]+", "-", str(title or "")).strip("-")[:80]
            jobs.append(_job(external_id=item.get("id"), title=title, location=self._location(item),
                             department=item.get("Industry"),
                             url=f"{self.careers_url(ref)}/{item.get('id')}/{words}?source=CareerSite",
                             posted=item.get("Date_Opened"), remote=bool(item.get("Remote_Job")),
                             country=item.get("Country"), description=item.get("Job_Description")))
        return ListResult(jobs=jobs[:max_jobs], company_name=self._company(page or ""),
                          complete=len(jobs) <= max_jobs)


PLATFORMS: dict[str, AtsPlatform] = {p.key: p for p in (
    Greenhouse(), Lever(), Workday(), Recruitee(), Ashby(), SmartRecruiters(), Workable(), Personio(), Breezy(),
    Teamtailor(), Rippling(), Gem(), Keka(), ZohoRecruit(), OracleCloud(),
)}


def get(platform: str) -> AtsPlatform | None:
    return PLATFORMS.get((platform or "").lower())


def parse_url(url: str) -> AtsRef | None:
    """Any supported careers / job / API URL -> AtsRef (None when no platform claims it)."""
    for platform in PLATFORMS.values():
        try:
            ref = platform.parse_url(url or "")
        except (ValueError, AttributeError):
            ref = None
        if ref and platform.valid(ref):
            return ref
    return None
