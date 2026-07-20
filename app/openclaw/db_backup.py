import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from . import config, gmail_client

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_service():
    # Same OAuth credentials as Gmail (drive.file scope added to gmail_client.SCOPES).
    return build("drive", "v3", credentials=gmail_client.load_credentials())


def _snapshot_db_bytes() -> bytes:
    """Safe point-in-time copy via sqlite's backup API — correct even while the
    live app is mid-write, unlike a raw file copy. Reads the snapshot back into
    memory and removes the temp file immediately: a second process (Drive's
    upload) reopening the same on-disk file right after sqlite closes it is a
    real Windows file-locking race (WinError 32); uploading bytes directly
    sidesteps it."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = os.path.join(tmp_dir, "snapshot.db")
        src = sqlite3.connect(config.DATABASE_PATH)
        dest = sqlite3.connect(tmp_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        with open(tmp_path, "rb") as f:
            return f.read()


def _get_or_create_subfolder(service, parent_id: str, name: str) -> str:
    query = f"'{parent_id}' in parents and name = '{name}' and mimeType = '{_FOLDER_MIME}' and trashed = false"
    found = service.files().list(q=query, fields="files(id)", spaces="drive").execute().get("files", [])
    if found:
        return found[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}, fields="id"
    ).execute()
    return folder["id"]


def _upload(service, data: bytes, name: str, parent_id: str, mimetype: str) -> None:
    media = MediaInMemoryUpload(data, mimetype=mimetype, resumable=False)
    service.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id").execute()


def _write_local_log(lines: list) -> None:
    os.makedirs(os.path.dirname(config.DB_BACKUP_LOCAL_LOG) or ".", exist_ok=True)
    with open(config.DB_BACKUP_LOCAL_LOG, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def backup_once() -> dict:
    """Snapshots the live DB and uploads it to the Drive backup folder, plus a
    per-run log file in a `logs` subfolder there. Always writes a local log
    line too — the durable, guaranteed-visible record if Drive itself is
    unreachable (CLAUDE.md: failures must be visible, never silent)."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = config.DRIVE_BACKUP_PREFIX
    db_name = f"{prefix}_{timestamp}.db"
    log_name = f"{prefix}_{timestamp}.log"
    lines = [f"{datetime.now(timezone.utc).isoformat()} backup start: {db_name}"]
    result = {"ok": False, "name": db_name}

    try:
        db_bytes = _snapshot_db_bytes()
        lines.append(f"snapshot ok, {len(db_bytes)} bytes")

        service = _drive_service()
        _upload(service, db_bytes, db_name, config.DRIVE_BACKUP_FOLDER_ID, "application/x-sqlite3")
        lines.append(f"uploaded {db_name} to Drive folder {config.DRIVE_BACKUP_FOLDER_ID}")
        result["ok"] = True

        log_folder_id = _get_or_create_subfolder(service, config.DRIVE_BACKUP_FOLDER_ID, config.DRIVE_BACKUP_LOG_SUBFOLDER)
        lines.append("backup succeeded")
        _write_local_log(lines)
        try:
            _upload(service, "\n".join(lines).encode("utf-8"), log_name, log_folder_id, "text/plain")
        except Exception as log_exc:
            logger.error("Drive backup: log upload failed (backup itself succeeded): %s", log_exc)
    except Exception as exc:
        lines.append(f"BACKUP FAILED: {exc!r}")
        _write_local_log(lines)
        logger.error("Drive backup failed: %s", exc)

    return result
