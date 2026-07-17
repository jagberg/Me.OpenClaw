from datetime import datetime, timezone

from . import config, db, gmail_client, tasks
from .scheduler import scheduler


def _already_processed(message_id: str) -> bool:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row is not None


def _mark_processed(message_id: str, task_id: int | None) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_emails (message_id, processed_at, task_id) VALUES (?, ?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat(), task_id),
        )


def poll_once() -> None:
    """Polls Gmail, ingests unseen messages as candidate tasks. Raises on Gemini/API failure —
    APScheduler logs and retries next interval; unprocessed messages stay unmarked so they're
    retried too."""
    service = gmail_client.build_service()
    response = service.users().messages().list(userId="me", maxResults=20, labelIds=["INBOX"]).execute()

    for item in response.get("messages", []):
        message_id = item["id"]
        if _already_processed(message_id):
            continue

        message = service.users().messages().get(
            userId="me", id=message_id, format="metadata", metadataHeaders=["Subject"]
        ).execute()
        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "(no subject)")
        snippet = message.get("snippet", "")
        description = f"{subject}: {snippet}"

        task_id = tasks.ingest_candidate(description, message_id)
        _mark_processed(message_id, task_id)


def start_polling() -> None:
    scheduler.add_job(
        poll_once,
        "interval",
        minutes=config.GMAIL_POLL_INTERVAL_MINUTES,
        id="gmail-poll",
        replace_existing=True,
    )
