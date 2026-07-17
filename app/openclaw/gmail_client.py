import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from . import config, ssl_compat

ssl_compat.patch_requests_to_use_os_trust_store()

# gmail.send alone does NOT cover drafts.create (confirmed live: 403 insufficient
# scope) — drafts need gmail.compose. Requires re-running scripts/gmail_auth.py.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]


def load_credentials() -> Credentials:
    if not os.path.exists(config.GMAIL_TOKEN_PATH):
        raise RuntimeError(
            f"No Gmail token at {config.GMAIL_TOKEN_PATH}. Run scripts/gmail_auth.py once to authorize."
        )
    creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(config.GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def build_service():
    return build("gmail", "v1", credentials=load_credentials())
