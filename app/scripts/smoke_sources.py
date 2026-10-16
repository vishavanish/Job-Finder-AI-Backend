"""
Live smoke check for the free job sources (not run in CI — it calls the
real sites). Prints how many jobs each source returned or why it failed.

    python -m app.scripts.smoke_sources                 # keyless feeds + ATS boards
    python -m app.scripts.smoke_sources --keyword "Data Engineer"

Keyed APIs (Adzuna, Jooble, Careerjet) run only when their key is in .env
(ADZUNA_API_KEY="APP_ID:APP_KEY", JOOBLE_API_KEY, CAREERJET_API_KEY).
Nothing is written to the database or Redis.
"""
from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.providers.job_sources import aggregators, feeds
from app.providers.job_sources.base import SourceQuery, geo_allows, matches_keyword
from app.services import company_ats_sources

ATS_SAMPLES = {"ashby": "ramp", "recruitee": "bunq", "workable": "huggingface", "personio": "personio",
               "breezy": "breezy"}
KEY_SETTINGS = {"adzuna": "ADZUNA_API_KEY", "jooble": "JOOBLE_API_KEY", "careerjet": "CAREERJET_API_KEY"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="Python Developer")
    parser.add_argument("--location", default="Bengaluru, India")
    parser.add_argument("--country", default="India")
    args = parser.parse_args()

    print(f"feeds (keyword '{args.keyword}', open to {args.country}):")
    for key, fetch in feeds.FEEDS.items():
        try:
            jobs = fetch()
            fit = [j for j in jobs if geo_allows(j["_geo"], args.country) and matches_keyword(j, args.keyword, j["_tags"])]
            print(f"  {key:10} ok   {len(jobs):4} in feed, {len(fit):3} match")
        except Exception as e:  # noqa: BLE001
            print(f"  {key:10} FAIL {e}")

    print("keyed APIs:")
    settings = get_settings()
    query = SourceQuery(keyword=args.keyword, location=args.location, country=args.country, max_age_hours=72)
    for key, search in aggregators.KEYWORD_APIS.items():
        api_key = getattr(settings, KEY_SETTINGS[key], "")
        if not api_key:
            print(f"  {key:10} skip (no {KEY_SETTINGS[key]} in .env)")
            continue
        try:
            print(f"  {key:10} ok   {len(search(query, api_key)):4} jobs")
        except Exception as e:  # noqa: BLE001
            print(f"  {key:10} FAIL {e}")

    print("company ATS boards:")
    for platform, slug in ATS_SAMPLES.items():
        notes: list[str] = []
        jobs = company_ats_sources.SLUG_FETCHERS[platform](slug, notes.append)
        print(f"  {platform:10} {'ok  ' if jobs else 'FAIL'} {len(jobs):4} jobs ({slug})"
              + ("" if jobs else f" — {'; '.join(notes)}"))


if __name__ == "__main__":
    main()
