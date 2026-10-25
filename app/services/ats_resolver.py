"""
app/services/ats_resolver.py
-------------------------------
The company resolver (QA3 §A): company names seen in any search result
(LinkedIn, Indeed, Naukri...) become registry companies without anyone
adding them by hand.

For each name not in the registry and not tried in the last
ATS_RESOLVER_RETRY_DAYS:

  1. website fingerprint — when the job gave the company's website or a
     non-board apply page: fetch it (and /careers, /jobs) and look for links,
     iframes or scripts on a supported ATS host (platforms.parse_url).
     Catches Workday, which can't be guessed. Only public hosts (SSRF guard),
     5 s, at most 2 redirects, the first 300 kB.
  2. slug probe — up to 3 slugs made from the name, one cheap list call on
     each guessable platform (Greenhouse, Lever, Ashby, SmartRecruiters,
     Recruitee, Workable, Breezy, Teamtailor), through the crawler's host
     slots.
  3. name check — a slug hit counts only when the board's own name matches
     the searched name (legal suffixes and filler words dropped, ≥ 80 %
     word overlap). A website hit is trusted (it is the company's own page)
     as long as the board answers.

Every outcome is stored in `ats_probes` (company or none). New companies get
origin "resolver" and are due for a crawl at once. Runs as a Celery task on
the maintenance queue after each keyword search; never touches the search.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.ats import AtsCompany, AtsProbe
from app.providers.ats import platforms
from app.providers.ats.platforms import AtsRef, GoneError
from app.providers.job_sources import base
from app.providers.job_sources.base import SourceError
from app.services import ats_crawler, ats_registry
from app.services.job_dedupe import normalize_company

logger = logging.getLogger("job_finder_api.ats_resolver")

GUESSABLE = ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee", "workable", "breezy", "teamtailor")
# Job boards and aggregators: their pages are never a company's own site.
BOARD_HOSTS = ("linkedin.com", "indeed.", "naukri.com", "glassdoor.", "google.com", "foundit.", "instahyre.com",
               "adzuna.", "jooble.", "careerjet.", "remoteok.", "remotive.", "himalayas.app", "jobicy.com",
               "weworkremotely.com", "ycombinator.com", "themuse.com", "arbeitnow.com", "monster.", "shine.com",
               "timesjobs.com", "wellfound.com", "angel.co", "cutshort.io", "hirist.", "iimjobs.com")
# Words dropped before comparing names: "Razorpay Software Pvt Ltd" == "Razorpay".
_FILLER = {"technologies", "technology", "tech", "labs", "lab", "software", "solutions", "systems", "services",
           "india", "global", "group", "holdings", "the", "and", "hq", "careers", "jobs", "io", "ai", "app", "inc",
           "pvt", "private", "ltd", "limited", "llc", "llp", "corp", "corporation", "co", "company", "plc", "gmbh"}
_URL = re.compile(r"""(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}[^\s"'<>)\\]*""", re.I)
_MAX_PAGE = 300_000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def name_tokens(name: str) -> set[str]:
    words = normalize_company(name).split()
    kept = {w for w in words if w not in _FILLER}
    return kept or set(words)


def names_match(searched: str, board: str | None) -> bool:
    if not board:
        return False
    a, b = name_tokens(searched), name_tokens(board)
    if not a or not b:
        return False
    if a == b or "".join(sorted(a)) == "".join(sorted(b)) or "".join(normalize_company(searched).split()) == \
            "".join(normalize_company(board).split()):
        return True
    return len(a & b) / len(a | b) >= 0.8


def slug_candidates(name: str) -> list[str]:
    """"Acme Technologies Pvt Ltd" -> ["acmetechnologies", "acme-technologies", "acme"] (≤ 3, valid slugs)."""
    words = normalize_company(name).split()
    core = [w for w in words if w not in _FILLER] or words
    out: list[str] = []
    for slug in ("".join(words), "-".join(words), "".join(core), "-".join(core)):
        if slug and slug not in out and platforms.valid_subdomain(slug):
            out.append(slug)
    return out[:3]


def _is_board(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(b in host for b in BOARD_HOSTS)


def candidates(db: Session, jobs: list[dict], limit: int) -> list[dict]:
    """Names (with any company website / own apply page) from search results that are not in the registry
    and were not tried recently. Each: {name, name_norm, urls}."""
    cfg = get_settings()
    by_norm: dict[str, dict] = {}
    for job in jobs:
        name = (job.get("company") or "").strip()
        norm = normalize_company(name)
        if not norm or len(norm) < 2:
            continue
        item = by_norm.setdefault(norm, {"name": name[:255], "name_norm": norm[:255], "urls": []})
        for url in (job.get("company_url"), job.get("apply_url")):
            if url and url.startswith(("http://", "https://")) and not _is_board(url) \
                    and not platforms.parse_url(url) and url not in item["urls"] and len(item["urls"]) < 2:
                item["urls"].append(url)
    if not by_norm:
        return []
    norms = list(by_norm)
    retry_before = _now() - timedelta(days=cfg.ATS_RESOLVER_RETRY_DAYS)
    tried = {n for (n, company_id, checked) in db.query(AtsProbe.name_norm, AtsProbe.company_id, AtsProbe.checked_at)
             .filter(AtsProbe.name_norm.in_(norms)).all() if company_id or checked > retry_before}
    known = {normalize_company(n) for (n,) in db.query(AtsCompany.name).filter(AtsCompany.active.is_(True)).all()}
    return [item for norm, item in by_norm.items() if norm not in tried and norm not in known][:limit]


# ---------------------------------------------------------------- website fingerprint

def _fetch_page(url: str) -> str:
    """GET a public page: no private hosts, ≤ 2 redirects (each re-checked), first 300 kB. "" on any failure."""
    for _ in range(3):
        host = (urlparse(url).hostname or "").lower()
        if urlparse(url).scheme not in ("http", "https") or not platforms.public_host(host):
            return ""
        try:
            resp = base.request("GET", url, headers={"Accept": "text/html"}, allow_redirects=False, timeout=5)
        except requests.RequestException:
            return ""
        if 300 <= resp.status_code < 400:
            nxt = (resp.headers or {}).get("Location") or (resp.headers or {}).get("location")
            if not nxt:
                return ""
            url = urljoin(url, nxt)
            if platforms.parse_url(url):
                return f'<a href="{url}"></a>'  # redirected straight to the ATS board
            continue
        if resp.status_code != 200:
            return ""
        return (resp.text or "")[:_MAX_PAGE]
    return ""


def refs_in_page(text: str) -> list[AtsRef]:
    """Supported ATS boards linked from a page, most mentioned first."""
    counts: dict[AtsRef, int] = {}
    for m in _URL.finditer(text or ""):
        raw = m.group(0)
        url = f"https:{raw}" if raw.startswith("//") else raw
        ref = platforms.parse_url(url.replace("&amp;", "&"))
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    return sorted(counts, key=counts.get, reverse=True)


def from_website(urls: list[str]) -> AtsRef | None:
    pages: list[str] = []
    for url in urls:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        for candidate in (url, f"{root}/careers", f"{root}/jobs"):
            if candidate not in pages:
                pages.append(candidate)
    for page in pages[:4]:
        found = refs_in_page(_fetch_page(page))
        if found:
            return found[0]
    return None


# ---------------------------------------------------------------- probing

def _slot_call(host: str, fn: Callable[[], object], wait_seconds: float = 8.0):
    """Runs `fn` inside a crawler host slot, waiting up to `wait_seconds` for one."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with ats_crawler.host_slot(host):
                return fn()
        except ats_crawler.HostBusy:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def _api_host(ref: AtsRef) -> str:
    return ats_crawler.api_host(AtsCompany(platform=ref.platform, slug=ref.slug, host=ref.host))


def probe(ref: AtsRef) -> tuple[bool, str | None]:
    """(board has open jobs, the board's own name). GoneError / SourceError propagate."""
    impl = platforms.get(ref.platform)

    def call():
        result = impl.list_jobs(ref, max_jobs=1)
        name = result.company_name
        if not name and result.jobs:
            try:
                name = impl.board_name(ref, result.jobs)
            except SourceError:
                name = None
        return bool(result.jobs), name

    return _slot_call(_api_host(ref), call)


def resolve_one(item: dict, enabled: set[str]) -> tuple[AtsRef | None, str | None, str | None, bool]:
    """(ref, board name, via, complete). complete=False when a check could not run (busy/errors) — then the
    miss is not stored, so the next search tries again."""
    complete = True
    if item["urls"]:
        ref = from_website(item["urls"])
        if ref and ref.platform in enabled:
            try:
                _, board = probe(ref)  # the company's own page links it: trusted even with no open jobs
                return ref, board, "website", True
            except GoneError:
                pass
            except (SourceError, ats_crawler.HostBusy):
                complete = False
    for slug in slug_candidates(item["name"]):
        for platform in GUESSABLE:
            if platform not in enabled:
                continue
            ref = AtsRef(platform, slug)
            if not platforms.get(platform).valid(ref):
                continue
            try:
                has_jobs, board = probe(ref)
            except ats_crawler.HostBusy:
                complete = False
                continue
            except SourceError:  # GoneError, or a guessed subdomain that doesn't resolve: not there
                continue
            if has_jobs and names_match(item["name"], board):
                return ref, board, "slug", True
    return None, None, None, complete


def resolve(session_factory: Callable[[], Session], jobs: list[dict], *,
            progress: Callable[[str], None] = lambda m: None) -> dict:
    """Resolves up to ATS_RESOLVER_MAX_NAMES new company names from `jobs` within ATS_RESOLVER_SECONDS."""
    cfg = get_settings()
    counts = {"checked": 0, "found": 0, "added": 0, "none": 0, "skipped": 0}
    if not cfg.ATS_RESOLVER_ENABLED:
        return counts
    db = session_factory()
    try:
        items = candidates(db, jobs, cfg.ATS_RESOLVER_MAX_NAMES)
        enabled = ats_registry.enabled_platforms(db)
    finally:
        db.close()
    r = get_redis()
    items = [i for i in items if r.set(f"ats:resolving:{i['name_norm']}", "1", nx=True, ex=600)]
    if not items:
        return counts
    progress(f"looking up career pages for {len(items)} companies")
    deadline = time.monotonic() + cfg.ATS_RESOLVER_SECONDS

    def one(item: dict) -> None:
        if time.monotonic() > deadline:
            counts["skipped"] += 1
            return
        try:
            ref, board, via, complete = resolve_one(item, enabled)
        except Exception:  # noqa: BLE001 — one odd site must not stop the rest
            logger.exception("ats resolver failed for %r", item["name"])
            counts["skipped"] += 1
            return
        counts["checked"] += 1
        if ref is None and not complete:
            counts["skipped"] += 1
            return
        s = session_factory()
        try:
            company_id = None
            if ref is not None:
                company, created = ats_registry.get_or_create(s, ref, origin="resolver", name=board or item["name"])
                company_id = company.id
                counts["found"] += 1
                counts["added"] += int(created)
            else:
                counts["none"] += 1
            row = s.get(AtsProbe, item["name_norm"]) or AtsProbe(name_norm=item["name_norm"], name=item["name"])
            row.company_id, row.via, row.checked_at = company_id, via, _now()
            s.merge(row)
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
            logger.exception("ats resolver could not store %r", item["name"])
        finally:
            s.close()

    pool = ThreadPoolExecutor(max_workers=4)
    futures = [pool.submit(one, item) for item in items]
    wait(futures, timeout=max(1.0, deadline - time.monotonic() + 5))
    pool.shutdown(wait=False, cancel_futures=True)
    progress(f"career pages found for {counts['found']} of {counts['checked']} companies")
    return counts
