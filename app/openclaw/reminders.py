from datetime import datetime, timezone

from . import db
from .scheduler import scheduler


def schedule_reminder(task_id: int, when: datetime) -> int:
    job_id = f"reminder-task-{task_id}-{when.isoformat()}"
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (task_id, scheduled_at, status, job_id, created_at) "
            "VALUES (?, ?, 'scheduled', ?, ?)",
            (task_id, when.isoformat(), job_id, datetime.now(timezone.utc).isoformat()),
        )
        reminder_id = cur.lastrowid

    # misfire_grace_time=None: if the app was down when `when` passed, fire immediately
    # on restart instead of treating the run as missed. Needed for restart-safe reminders.
    scheduler.add_job(
        mark_due,
        "date",
        run_date=when,
        args=[reminder_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=None,
    )
    return reminder_id


def mark_due(reminder_id: int) -> None:
    with db.get_connection() as conn:
        conn.execute("UPDATE reminders SET status = 'due' WHERE id = ?", (reminder_id,))
