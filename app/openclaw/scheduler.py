from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from . import config

scheduler = BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{config.DATABASE_PATH}")}
)


def start() -> None:
    if not scheduler.running:
        scheduler.start()
