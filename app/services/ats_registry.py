"""
app/services/ats_registry.py
-------------------------------
The shared company registry for ATS Discovery (P6 plan §3.1).

How companies get in:
  1. seed file app/data/ats_seed.json (probed offline by
     app.scripts.discover_ats); loaded by seed() — the crawler tick also
     loads it once when the registry is empty;
  2. harvest(): apply links on any supported ATS host seen in other
     sources' results (LinkedIn's "apply on company site", feeds, alerts);
  3. a user adds a careers URL (POST /ats/companies);
  4. an admin adds or edits one.

A new company is due for a crawl at once; the crawl itself is the probe —
a slug that is not on the platform is switched off after ATS_GONE_AFTER
404s (P6 plan §3.2).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ats import AtsCompany, AtsPlatformSetting
from app.providers.ats import platforms
from app.providers.ats.platforms import AtsRef

logger = logging.getLogger("job_finder_api.ats_registry")

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "ats_seed.json"
HARVEST_MAX = 25


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ref_key(ref: AtsRef) -> str:
    parts = [ref.platform, ref.slug.lower()]
    if ref.platform == "workday":
        parts += [(ref.host or "").lower(), (ref.site or "").lower()]
    elif ref.host:
        parts.append(ref.host.lower())
    return ":".join(parts)[:400]


def to_ref(company: AtsCompany) -> AtsRef:
    return AtsRef(company.platform, company.slug, host=company.host, site=company.site)


def display_name(ref: AtsRef) -> str:
    return ref.slug.replace("-", " ").replace("_", " ").strip().title() or ref.slug


def get_or_create(db: Session, ref: AtsRef, *, origin: str, name: str | None = None,
                  user_id: str | None = None, commit: bool = True) -> tuple[AtsCompany, bool]:
    """(company, created). Existing companies are returned as they are (a switched-off one stays off)."""
    key = ref_key(ref)
    existing = db.query(AtsCompany).filter(AtsCompany.ref_key == key).first()
    if existing:
        return existing, False
    platform = platforms.get(ref.platform)
    row = AtsCompany(ref_key=key, platform=ref.platform, slug=ref.slug, host=ref.host, site=ref.site,
                     name=(name or display_name(ref))[:255], careers_url=platform.careers_url(ref), origin=origin,
                     active=True, added_by_user_id=user_id, job_count=0, fail_count=0, gone_count=0,
                     next_crawl_at=now(), created_at=now())
    db.add(row)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:  # added by someone else at the same moment
        db.rollback()
        return db.query(AtsCompany).filter(AtsCompany.ref_key == key).one(), False
    return row, True


def parse_user_url(url: str) -> AtsRef:
    """A careers / job URL a person typed -> AtsRef, or a 422 that says what is supported."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    ref = platforms.parse_url(url)
    if ref is None:
        names = ", ".join(p.name for p in platforms.PLATFORMS.values())
        raise HTTPException(422, {"code": "ats_unsupported",
                                  "message": f"That link isn't a careers page we can read yet. Supported: {names}.",
                                  "details": {}})
    if ref.host and ref.platform != "workday" and not platforms.public_host(ref.host):
        raise HTTPException(400, {"code": "ats_private_host",
                                  "message": "That address is not a public website.", "details": {}})
    return ref


def ref_from_parts(platform: str, slug: str) -> AtsRef:
    impl = platforms.get(platform)
    if impl is None:
        raise HTTPException(422, {"code": "ats_unknown_platform", "message": f"Unknown platform '{platform}'.",
                                  "details": {}})
    ref = AtsRef(impl.key, (slug or "").strip())
    if impl.key == "workday" or not impl.valid(ref):
        raise HTTPException(422, {"code": "ats_bad_slug",
                                  "message": "Give the careers page link instead (Workday needs the full link)."
                                  if impl.key == "workday" else "That company id has characters we can't use.",
                                  "details": {}})
    return ref


def harvest(db: Session, jobs: list[dict]) -> int:
    """Adds companies whose apply links point at a supported ATS. Cheap: URL parsing + one lookup."""
    refs: dict[str, AtsRef] = {}
    for job in jobs:
        for url in (job.get("apply_url"), job.get("url")):
            ref = platforms.parse_url(url) if url else None
            if ref:
                refs.setdefault(ref_key(ref), ref)
            if len(refs) >= HARVEST_MAX * 4:
                break
    if not refs:
        return 0
    known = {k for (k,) in db.query(AtsCompany.ref_key).filter(AtsCompany.ref_key.in_(list(refs))).all()}
    added = 0
    for key, ref in refs.items():
        if key in known or added >= HARVEST_MAX:
            continue
        _, created = get_or_create(db, ref, origin="pipeline", commit=False)
        added += int(created)
    db.commit()
    return added


def seed_platforms(path: Path | None = None) -> set[str]:
    """Platforms the seed file names (read once per file version)."""
    path = path or SEED_FILE
    if not path.exists():
        return set()
    return _seed_platforms(str(path), path.stat().st_mtime)


@lru_cache(maxsize=4)
def _seed_platforms(path: str, mtime: float) -> set[str]:
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    return {e["platform"] for e in entries if isinstance(e, dict) and e.get("platform")}


def needs_seed(db: Session) -> bool:
    """True when the registry is empty, or the seed file names a platform with no company yet (a platform
    added after the first load, e.g. Keka / Zoho Recruit in QA7)."""
    have = {p for (p,) in db.query(AtsCompany.platform).distinct().all()}
    return not have or bool(seed_platforms() - have)


def seed(db: Session, path: Path | None = None) -> int:
    """Loads the seed file (idempotent). Entries: {"url": ...} or {"platform", "slug", "host"?, "site"?, "name"?}."""
    path = path or SEED_FILE
    if not path.exists():
        return 0
    entries = json.loads(path.read_text(encoding="utf-8"))
    keys = {k for (k,) in db.query(AtsCompany.ref_key).all()}
    added = 0
    for entry in entries:
        try:
            if entry.get("url"):
                ref = platforms.parse_url(entry["url"])
            else:
                ref = AtsRef(entry["platform"], entry["slug"], host=entry.get("host"), site=entry.get("site"))
                impl = platforms.get(ref.platform)
                ref = ref if impl and impl.valid(ref) else None
        except (KeyError, TypeError):
            ref = None
        if ref is None or ref_key(ref) in keys:
            continue
        get_or_create(db, ref, origin="seed", name=entry.get("name"), commit=False)
        keys.add(ref_key(ref))
        added += 1
    db.commit()
    logger.info("ats seed: %s companies added", added)
    return added


def platform_settings(db: Session) -> dict[str, AtsPlatformSetting]:
    return {r.platform: r for r in db.query(AtsPlatformSetting).all()}


def enabled_platforms(db: Session) -> set[str]:
    rows = platform_settings(db)
    return {p for p in platforms.PLATFORMS if rows.get(p) is None or rows[p].enabled}
