"""Run one Drive DB backup immediately (the scheduled job runs this same
function twice a day — see openclaw/db_backup.py). Useful to test after
re-running gmail_auth.py, or to trigger an ad-hoc backup:
    python scripts/backup_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openclaw import config
from openclaw.db_backup import backup_once


def main() -> None:
    result = backup_once()
    if result["ok"]:
        print(f"Backup OK: {result['name']}")
    else:
        print(f"Backup FAILED — see {config.DB_BACKUP_LOCAL_LOG}")
        sys.exit(1)


if __name__ == "__main__":
    main()
