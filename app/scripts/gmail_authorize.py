"""
One-time OAuth authorization script for a Gmail account used by the
rejection-email poller.

Usage (from the project root, i.e. job_finder_api/):
    python app/scripts/gmail_authorize.py primary
    python app/scripts/gmail_authorize.py secondary

Produces token_<label>.json in the credentials directory (see
GMAIL_CREDENTIALS_PATH in Settings), which fetch_candidates_for_account()
loads later.

NOTE: This file must live at app/scripts/gmail_authorize.py. BASE_DIR below
is computed relative to the *project root* (two levels up from this file:
app/scripts/gmail_authorize.py -> app/scripts -> app -> project root), not
relative to the current working directory, so it works regardless of where
you run `python` from.
"""

import sys
from pathlib import Path

# --- Path setup -------------------------------------------------------
# app/scripts/gmail_authorize.py
#   .parent        -> app/scripts
#   .parent.parent  -> app
#   .parent.parent.parent -> project root (job_finder_api/)
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parent.parent.parent

# Make sure `app` is importable regardless of CWD.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402

from app.core.config import get_settings  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    if len(sys.argv) != 2:
        print("Usage: python app/scripts/gmail_authorize.py <label>")
        print("  <label> must be one of the values in GMAIL_ACCOUNT_LABELS, e.g. primary / secondary")
        sys.exit(1)

    label = sys.argv[1]

    settings = get_settings()

    if label not in settings.gmail_account_labels_list:
        print(f"Warning: '{label}' is not listed in GMAIL_ACCOUNT_LABELS "
              f"({settings.gmail_account_labels_list}). Continuing anyway, but "
              f"fetch_all_candidates() won't pick this token up unless you add it.")

    # GMAIL_CREDENTIALS_PATH points at credentials.json itself (see config.py),
    # not a directory. Token files are stored alongside it.
    credentials_json = Path(settings.GMAIL_CREDENTIALS_PATH)
    if not credentials_json.is_absolute():
        credentials_json = PROJECT_ROOT / credentials_json
    creds_dir = credentials_json.parent
    creds_dir.mkdir(parents=True, exist_ok=True)

    token_path = creds_dir / f"token_{label}.json"

    if not credentials_json.exists():
        print(f"ERROR: {credentials_json} not found.")
        print("Download the OAuth Desktop client credentials from Google Cloud "
              "Console and save them at that path (same credentials.json is "
              "reused for every account/label).")
        sys.exit(1)

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_json), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())
        print(f"Saved token for '{label}' -> {token_path}")
    else:
        print(f"Existing token for '{label}' is still valid -> {token_path}")


if __name__ == "__main__":
    main()