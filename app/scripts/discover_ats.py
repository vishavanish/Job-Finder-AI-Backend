"""
Builds the ATS Discovery seed file (P6 plan §3.1): finds company boards on
the supported ATS platforms, probes each one live, and writes the ones
that answer with open jobs to app/data/ats_seed.json (merged with what is
already there). Offline / admin use only — never in the request path.

Sources of candidates:
  --candidates FILE   one per line: a careers URL, or platform:slug
  --commoncrawl       Common Crawl URL index for each platform's host
                      pattern (often slow / 504 — retried, skipped on failure)
  --from-db           companies already in the DATABASE_URL registry

Run from job_finder_api/:
    python -m app.scripts.discover_ats --candidates app/data/ats_candidates.txt --commoncrawl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from app.providers.ats import platforms
from app.providers.ats.platforms import AtsRef, GoneError
from app.providers.job_sources.base import SourceError
from app.services.ats_registry import SEED_FILE, ref_key

CC_PATTERNS = [
    "job-boards.greenhouse.io/*", "boards.greenhouse.io/*", "jobs.lever.co/*", "jobs.ashbyhq.com/*",
    "*.recruitee.com", "*.myworkdayjobs.com", "jobs.smartrecruiters.com/*", "apply.workable.com/*",
    "*.teamtailor.com", "*.breezy.hr", "*.jobs.personio.de", "ats.rippling.com/*", "jobs.gem.com/*",
]


def _from_line(line: str) -> AtsRef | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line or "/" in line or line.count(".") >= 2:
        return platforms.parse_url(line if "://" in line else f"https://{line}")
    if ":" in line:
        platform, slug = line.split(":", 1)
        impl = platforms.get(platform)
        ref = AtsRef(platform.strip().lower(), slug.strip()) if impl else None
        return ref if ref and impl.valid(ref) else None
    return None


def _cc_get(index: str, params: dict, log, label: str) -> requests.Response | None:
    for attempt in range(3):
        try:
            resp = requests.get(index, params=params, timeout=90)
            if resp.status_code == 200:
                return resp
            raise SourceError(f"HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            log(f"Common Crawl {label}: attempt {attempt + 1} failed ({type(e).__name__})")
            time.sleep(10 * (attempt + 1))
    return None


def common_crawl(limit_per_page: int, pages: int, log) -> list[AtsRef]:
    """Results come back in URL order, so one page only covers companies early in the alphabet:
    sample `pages` pages spread over each pattern's whole result set instead."""
    try:
        index = requests.get("https://index.commoncrawl.org/collinfo.json", timeout=30).json()[0]["cdx-api"]
    except Exception as e:  # noqa: BLE001
        log(f"Common Crawl index list unavailable: {type(e).__name__}")
        return []
    refs: list[AtsRef] = []
    for pattern in CC_PATTERNS:
        meta = _cc_get(index, {"url": pattern, "showNumPages": "true", "output": "json"}, log, pattern)
        try:
            total = int(json.loads(meta.text).get("pages", 1)) if meta else 1
        except (ValueError, AttributeError):
            total = 1
        picks = sorted({round(i * (total - 1) / max(1, pages - 1)) for i in range(min(pages, total))})
        found = 0
        for page in picks:
            resp = _cc_get(index, {"url": pattern, "output": "json", "fl": "url", "limit": limit_per_page,
                                   "filter": "status:200", "page": page}, log, f"{pattern} page {page}")
            for line in (resp.text.splitlines() if resp else []):
                try:
                    ref = platforms.parse_url(json.loads(line)["url"])
                except (ValueError, KeyError):
                    continue
                if ref:
                    refs.append(ref)
                    found += 1
        log(f"Common Crawl {pattern}: {found} URLs from {len(picks)} of {total} pages")
    return refs


def probe(ref: AtsRef) -> dict | None:
    impl = platforms.get(ref.platform)
    try:
        result = impl.list_jobs(ref, max_jobs=60)
    except (GoneError, SourceError):
        return None
    except Exception:  # noqa: BLE001
        return None
    if not result.jobs:
        return None
    entry = {"platform": ref.platform, "slug": ref.slug, "jobs": len(result.jobs)}
    if ref.host:
        entry["host"] = ref.host
    if ref.site:
        entry["site"] = ref.site
    if result.company_name:
        entry["name"] = result.company_name.strip()
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", type=Path, action="append", default=[])
    ap.add_argument("--commoncrawl", action="store_true")
    ap.add_argument("--cc-limit", type=int, default=3000, help="URLs per sampled page")
    ap.add_argument("--cc-pages", type=int, default=8, help="pages sampled per platform pattern")
    ap.add_argument("--from-db", action="store_true")
    ap.add_argument("--out", type=Path, default=SEED_FILE)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731

    existing = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else []
    known = {ref_key(AtsRef(e["platform"], e["slug"], host=e.get("host"), site=e.get("site"))): e for e in existing}
    refs: dict[str, AtsRef] = {}
    for path in args.candidates:
        for line in path.read_text(encoding="utf-8").splitlines():
            ref = _from_line(line)
            if ref:
                refs.setdefault(ref_key(ref), ref)
    if args.commoncrawl:
        for ref in common_crawl(args.cc_limit, args.cc_pages, log):
            refs.setdefault(ref_key(ref), ref)
    if args.from_db:
        from app.core.db import SessionLocal
        from app.models.ats import AtsCompany
        from app.services.ats_registry import to_ref
        db = SessionLocal()
        try:
            for c in db.query(AtsCompany).filter(AtsCompany.active.is_(True)).all():
                refs.setdefault(c.ref_key, to_ref(c))
        finally:
            db.close()
    todo = [r for k, r in refs.items() if k not in known]
    log(f"{len(refs)} candidates, {len(todo)} new to probe")
    added = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, entry in enumerate(pool.map(probe, todo), 1):
            if entry:
                entry.pop("jobs", None)
                known[ref_key(AtsRef(entry["platform"], entry["slug"], host=entry.get("host"),
                                     site=entry.get("site")))] = entry
                added += 1
            if i % 50 == 0:
                log(f"probed {i}/{len(todo)}, {added} answering")
    seed = sorted(known.values(), key=lambda e: (e["platform"], e.get("name", e["slug"]).lower()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(seed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"seed: {len(seed)} companies ({added} new) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
