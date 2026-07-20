import base64
import os
from io import BytesIO

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pypdf import PdfReader

from . import config, ssl_compat

ssl_compat.patch_requests_to_use_os_trust_store()

# gmail.send alone does NOT cover drafts.create (confirmed live: 403 insufficient
# scope) — drafts need gmail.compose. Requires re-running scripts/gmail_auth.py.
# drive.file added for db_backup.py — scoped to files this app creates, not
# full Drive access. Adding it to an existing token also requires re-running
# scripts/gmail_auth.py once (new scope needs fresh consent).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
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


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _message_text(message: dict) -> str:
    """Best-effort plain-text body extraction; falls back to the snippet if no
    text/plain part is found."""
    payload = message.get("payload", {})
    parts = payload.get("parts") or [payload]
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_part(part["body"]["data"])
    return message.get("snippet", "")


def _iter_attachment_parts(payload: dict):
    for part in payload.get("parts") or []:
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            yield part
        if part.get("parts"):
            yield from _iter_attachment_parts(part)


def _pdf_attachment_text(service, message_id: str, attachment_id: str) -> str:
    attachment = service.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()
    data = base64.urlsafe_b64decode(attachment["data"] + "==")
    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def full_message_text(service, message: dict) -> str:
    """Body text plus any PDF attachment text — invoices and Petcover
    settlement breakdowns frequently live only in an attached PDF, not the
    body. Image attachments (PNG/JPG) are skipped: no OCR support."""
    text = _message_text(message)
    for part in _iter_attachment_parts(message.get("payload", {})):
        if part.get("mimeType") != "application/pdf":
            continue
        try:
            text += "\n" + _pdf_attachment_text(service, message["id"], part["body"]["attachmentId"])
        except Exception:
            continue  # unreadable attachment — fall back to whatever text we already have
    return text
