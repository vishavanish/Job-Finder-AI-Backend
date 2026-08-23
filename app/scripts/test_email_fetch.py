# app/scripts/test_email_fetch.py
"""
Manual sanity check for the Gmail poller — run this before wiring in LLM
classification, to confirm token(s) are valid and the pre-filter is
actually catching real candidate emails.

Usage (from the project root, i.e. job_finder_api/):
    python app/scripts/test_email_fetch.py
    python app/scripts/test_email_fetch.py --lookback 4320   # last 3 days
"""

import argparse
import sys
from pathlib import Path

# Same bootstrap pattern as gmail_authorize.py — resolve the project root
# relative to this file, not the current working directory, so it works
# regardless of where `python` is invoked from.
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.email_tracker import fetch_all_candidates  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lookback", type=int, default=1440,
        help="Minutes to look back (default 1440 = last 24h)."
    )
    args = parser.parse_args()

    candidates = fetch_all_candidates(lookback_minutes=args.lookback, progress=print)

    print(f"\n--- {len(candidates)} candidate(s) after pre-filter ---")
    if not candidates:
        print("No candidates found. If you expected some, check:")
        print("  - token_<label>.json exists and is valid for each account")
        print("  - GMAIL_ACCOUNT_LABELS in .env matches your actual token file labels")
        print("  - the lookback window actually covers when the email arrived")
        return

    for c in candidates:
        print(f"[{c.account_label}] {c.sender} | {c.subject}")
        print(f"    snippet: {c.snippet[:120]}")


if __name__ == "__main__":
    main()