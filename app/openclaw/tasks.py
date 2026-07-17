import json
from datetime import datetime, timezone

from . import db, gemini
from .reminders import schedule_reminder

FOLLOW_UP_PROMPT = """Extract a follow-up date/time from this task, if any is mentioned.
Respond with ONLY strict JSON: {{"follow_up_at": "<ISO 8601 datetime, or null>"}}.
Current date/time (UTC) is {now}.

Task: {description}
"""


def _extract_follow_up(description: str) -> datetime | None:
    prompt = FOLLOW_UP_PROMPT.format(now=datetime.now(timezone.utc).isoformat(), description=description)
    raw = gemini.extract(prompt, purpose="follow_up_extraction")
    # Gemini often wraps JSON in a ```json ... ``` markdown fence; pull out the
    # {...} object itself rather than relying on the fence format.
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(raw[start : end + 1])
        value = data.get("follow_up_at")
        return datetime.fromisoformat(value) if value else None
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


def create_task(description: str, source: str = "chat", source_message_id: str | None = None) -> int:
    """Raises gemini.GeminiUnavailableError if extraction fails — caller must surface it, not swallow it."""
    follow_up_at = _extract_follow_up(description)

    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (description, status, source, source_message_id, follow_up_at, created_at) "
            "VALUES (?, 'open', ?, ?, ?, ?)",
            (
                description,
                source,
                source_message_id,
                follow_up_at.isoformat() if follow_up_at else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        task_id = cur.lastrowid

    if follow_up_at:
        schedule_reminder(task_id, follow_up_at)

    return task_id


def record_outcome(task_id: int, outcome: str) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET outcome = ?, outcome_at = ?, status = 'closed' WHERE id = ?",
            (outcome, datetime.now(timezone.utc).isoformat(), task_id),
        )


def ingest_candidate(description: str, source_message_id: str) -> int:
    return create_task(description, source="email", source_message_id=source_message_id)
