"""One-time interactive Gmail OAuth consent flow.

Run locally (not inside Docker — needs a browser) once per machine:
    python scripts/gmail_auth.py

Requires GMAIL_CREDENTIALS_PATH (OAuth client secret downloaded from Google Cloud
Console) to exist; writes the resulting refresh token to GMAIL_TOKEN_PATH.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from openclaw import config
from openclaw.gmail_client import SCOPES


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(config.GMAIL_CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    Path(config.GMAIL_TOKEN_PATH).write_text(creds.to_json())
    print(f"Saved Gmail token to {config.GMAIL_TOKEN_PATH}")


if __name__ == "__main__":
    main()
