"""Reminders, and the one job cron cannot express.

Every other scheduled thing here is a fixed cadence — a 15-minute tick, a daily
nudge, a weekly chase — and each maps to exactly one cron entry. A reminder does
not: it is a one-shot at an arbitrary minute chosen at capture time, and there is
no cron expression for "whenever the user happens to say". APScheduler modelled
that directly with a `date` job per reminder.

So the port is a **sweep**, not a translation: cron fires a fixed cadence and the
sweep marks everything now due. Justin chose to fold it into the existing
15-minute tick (2026-08-04) rather than add a minute-resolution cron entry, which
makes the worst-case lateness one tick. That is the deliberate trade: a reminder
set for 3:00 surfaces by 3:15.

**Nothing is ever dropped for being late.** `misfire_grace_time=None` was the
APScheduler spelling of the same rule — a reminder due while the machine slept
fired on restart rather than being treated as missed. A sweep gets that for free,
because it asks the DB what is due rather than remembering what it meant to do.
Lateness is reported instead of hidden (Justin's call, 2026-08-04): the sweep logs
how overdue each one was, and the dashboard says so, so a reminder that surfaces
three days late is not mistaken for one set this morning.
"""
import logging
from datetime import datetime, timezone

from . import db
from .scheduler import scheduler

logger = logging.getLogger(__name__)


def schedule_reminder(task_id: int, when: datetime) -> int:
    job_id = f"reminder-task-{task_id}-{when.isoformat()}"
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (task_id, scheduled_at, status, job_id, created_at) "
            "VALUES (?, ?, 'scheduled', ?, ?)",
            (task_id, when.isoformat(), job_id, datetime.now(timezone.utc).isoformat()),
        )
        reminder_id = cur.lastrowid

    # The APScheduler job is now belt-and-braces, not the mechanism: `sweep_due`
    # marks the same row from the tick, and whichever runs first wins (the sweep
    # only looks at `status = 'scheduled'`). It stays while
    # `config.SCHEDULER_ENABLED` can put the in-process scheduler back, and goes
    # with the rest of APScheduler in section 6.
    #
    # misfire_grace_time=None: if the app was down when `when` passed, fire
    # immediately on restart instead of treating the run as missed.
    if scheduler.running:
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


def sweep_due(now: datetime | None = None) -> int:
    """Mark every scheduled reminder whose time has passed. Returns the count.

    Runs from the pipeline tick. Idempotent by construction — the `WHERE`
    restricts to `status = 'scheduled'`, so a duplicated cron delivery (5.4) or an
    overlapping tick marks nothing twice, and no `fired_at` column is needed for
    the guarantee. That matters practically: a new column means hand-run
    `ALTER TABLE` against the live DB, and the existing `status` already carries
    the fact.

    Lateness comes from `scheduled_at` rather than from a stored fire time, which
    the dashboard already has. One log line per swept reminder, because a reminder
    surfacing days late is worth a trace even though it is not a failure.
    """
    now = now or datetime.now(timezone.utc)
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, scheduled_at FROM reminders "
            "WHERE status = 'scheduled' AND scheduled_at <= ?",
            (now.isoformat(),),
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE reminders SET status = 'due' "
            "WHERE status = 'scheduled' AND scheduled_at <= ?",
            (now.isoformat(),),
        )

    for row in rows:
        logger.info("reminder %s due (%s)", row["id"], overdue_text(row["scheduled_at"], now))
    return len(rows)


def overdue_text(scheduled_at: str, now: datetime | None = None) -> str:
    """How late a reminder is, in words. Shared by the sweep's log and the
    dashboard so the two cannot disagree about what "late" means.

    Returns "on time" inside one tick's worth of lag — surfacing 4 minutes after
    the minute asked for is the design, not a delay worth naming.
    """
    now = now or datetime.now(timezone.utc)
    try:
        when = datetime.fromisoformat(scheduled_at)
    except ValueError:
        return "scheduled time unreadable"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    late = (now - when).total_seconds()
    if late < 15 * 60:
        return "on time"
    if late < 3600:
        return f"overdue by {int(late // 60)}m"
    if late < 86400:
        return f"overdue by {int(late // 3600)}h"
    return f"overdue by {int(late // 86400)}d"
