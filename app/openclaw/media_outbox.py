"""The one narrow path between the two containers.

The claim cards are Pillow renders — kept deliberately after Justin compared
them live against native markdown tables and chose them (11.3). Today they are
sent as **bytes** straight through python-telegram-bot. The gateway CLI takes a
**path**, and it will not send from just any path: `assertLocalMediaAllowed`
refuses anything outside a fixed set of roots, and `/tmp` is not among them
(14.1, 14.4). Sending the identical file from `<stateDir>/media/outbox` succeeds.

So a file has to exist somewhere both containers can see, and that somewhere has
to be inside one of the gateway's own roots. Hence one shared volume, mounted
`/data/outbox` in the app and `<stateDir>/media/outbox` read-only in the gateway.

**Two path spaces for one file, and that is the thing to hold onto.** The app
writes `/data/outbox/card-<id>.png`; the gateway must be told
`/home/node/.openclaw/media/outbox/card-<id>.png`. Handing the gateway the app's
path produces `Local media path is not under an allowed directory`, which reads
like a permissions problem and is actually a namespace one.

The mount point moved one level down, from `<stateDir>/media` to
`<stateDir>/media/outbox`, during `csv-upload-via-telegram` (2026-08). Read-only
at the OLD mount point shadowed the whole `media` tree, including
`media/inbound` — the directory the gateway itself downloads an inbound
Telegram document into. Nesting the outbox one level deeper leaves
`media/inbound` on the writable `gateway_state` volume, and the allowlist still
accepts it: the check is containment under `<stateDir>/media`, not an exact
path, so a file under `media/outbox/` is still inside the allowed root.

**Why not widen the allowlist instead.** `media.localRoots` accepts the string
`"any"`, which disables the check outright. It is one word and it is the whole
control. The isolation decision (7.0a) is that the gateway cannot see
`app/data`; an outbox it can read is the deliberate exception, and it stays
narrow — rendered cards and claim PDFs, nothing else, never a mount of the data
directory.
"""

import logging
import os
import secrets
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# How long a published file survives. It only has to outlive the CLI call that
# sends it — Telegram keeps its own copy once delivered, so nothing later reads
# these. Generous because the cost of a stale file is a few hundred KB and the
# cost of deleting one too early is a send that fails after the caller was told
# it succeeded.
TTL_SECONDS = 15 * 60


class OutboxError(RuntimeError):
    """The file could not be published, so the send must not be attempted."""


def _sweep(directory: Path) -> None:
    """Delete expired files. Called on publish rather than on a timer.

    ponytail: no scheduler, no cleanup thread. Publishing is the only event that
    matters here, and a directory nobody writes to does not need tidying.
    """
    cutoff = time.time() - TTL_SECONDS
    for path in directory.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:  # noqa: PERF203 — one unremovable file must not stop the sweep
            logger.warning("outbox: could not remove %s: %s", path.name, exc)


def publish(data: bytes, suffix: str = ".png", stem: str = "card") -> str:
    """Write bytes to the outbox; return the path **as the gateway sees it**.

    The return value is deliberately the gateway's path and not the app's, so a
    caller cannot accidentally hand the CLI a path it will refuse. Nothing in
    this app needs to read the file back.
    """
    directory = Path(config.MEDIA_OUTBOX_DIR)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutboxError(f"outbox directory {directory} is not usable: {exc}") from exc

    _sweep(directory)

    # A random component, not a claim id: these filenames end up in a directory
    # the gateway can read, and a predictable name is a small invitation.
    name = f"{stem}-{secrets.token_hex(8)}{suffix}"
    target = directory / name
    try:
        # Write-then-rename, so the gateway can never be pointed at a file that
        # is still being written. A truncated PNG sends "successfully" and
        # arrives corrupt, which is the failure shape this project keeps hitting.
        temp = directory / f".{name}.part"
        temp.write_bytes(data)
        os.replace(temp, target)
    except OSError as exc:
        raise OutboxError(f"could not write {name} to the outbox: {exc}") from exc

    logger.info("outbox: published %s (%d bytes)", name, len(data))
    return f"{config.MEDIA_OUTBOX_GATEWAY_DIR.rstrip('/')}/{name}"
