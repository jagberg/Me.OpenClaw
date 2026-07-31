from apscheduler.schedulers.background import BackgroundScheduler

# Default (in-memory) jobstore, deliberately. This was a SQLAlchemyJobStore writing
# an `apscheduler_jobs` table into the live DB, which bought nothing: every job is
# registered with `replace_existing=True` at startup (pipeline.start, reminders,
# gmail_ingest), and that recomputes next_run_time from the trigger, overwriting
# whatever was persisted. Measured 2026-07-31 — two boots against the same file
# produced two different next_run_times. The misfire handling the job definitions
# care about (a machine asleep with the process alive) is scheduler-side and
# unaffected. Dropping it removed the sqlalchemy dependency outright.
# If jobs ever stop being re-registered at startup, this has to go back.
scheduler = BackgroundScheduler()


def start() -> None:
    if not scheduler.running:
        scheduler.start()
