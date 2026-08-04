import base64
import html
import os
import re
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


def _walk_parts(part: dict):
    yield part
    for child in part.get("parts") or []:
        yield from _walk_parts(child)


_TAGS = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"(?i)</t[dh]>")
_ROW_END = re.compile(r"(?i)</tr>|<br\s*/?>|</p>|</div>|</h[1-6]>")
_SCRIPTS = re.compile(r"(?is)<(script|style).*?</\1>")


def _html_to_text(raw: str) -> str:
    """Enough HTML to read a table. Cells become ' | ', rows become newlines.

    Not a parser and not trying to be — the point is that a claims-relevant mail
    with no text/plain part still yields its figures instead of a 198-character
    snippet. Live case: Petcover's 29/07/2026 status table, the only document
    that states a treatment date per claim serial, is HTML-only.
    """
    text = _SCRIPTS.sub("", raw)
    text = _BLOCK_END.sub(" | ", text)
    text = _ROW_END.sub("\n", text)
    text = _TAGS.sub("", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip(" |").strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _message_text(message: dict) -> str:
    """Best-effort body extraction: text/plain, else the HTML part rendered down
    to text, else the snippet.

    The HTML fallback exists because "no text/plain part" used to degrade to a
    snippet silently — a truncated body that reads exactly like a short email.
    """
    payload = message.get("payload", {})
    for part in _walk_parts(payload):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_part(part["body"]["data"])
    for part in _walk_parts(payload):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return _html_to_text(_decode_part(part["body"]["data"]))
    return message.get("snippet", "")


def _iter_attachment_parts(payload: dict):
    for part in payload.get("parts") or []:
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            yield part
        if part.get("parts"):
            yield from _iter_attachment_parts(part)


def _pdf_attachment_text(service, message_id: str, attachment_id: str) -> str:
    attachment = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
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
            text += "\n" + _pdf_attachment_text(
                service, message["id"], part["body"]["attachmentId"]
            )
        except Exception:
            continue  # unreadable attachment — fall back to whatever text we already have
    return text
