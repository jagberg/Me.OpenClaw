"""Internal surface the OpenClaw gateway calls into.

Two callers, one shape: gateway cron invokes the scheduled jobs, and (later) the
event-bridge plugin posts inbound channel events. Everything here is a thin
wrapper over an existing entrypoint — no claims logic lives in this module, for
the same reason the Telegram commands are a thin adapter over the dashboard's
logic (they cannot be allowed to drift).

Auth is the shared secret; the host allowlist is defence in depth, not the
guarantee. A rejected request is logged loudly: a rejection that looked like
"no request arrived" is exactly the silent failure the project's rules forbid.
"""

import logging
import threading
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from . import config, gmail_ingest, pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal")

# Overlap protection. APScheduler refused an overlapping run for free
# (`max_instances` defaults to 1, and no job here overrides it); gateway cron
# has no such notion and will happily fire while the last run is still going.
# So this is a guarantee being rebuilt, not a new one — two `pipeline.run_once`
# calls would draft the same claims into two Gmail drafts, i.e. two Petcover
# submissions for one set of invoices.
#
# ponytail: plain in-process locks. Ceiling: they serialise within ONE process,
# which is exactly today's deployment — the Dockerfile runs uvicorn with no
# `--workers`, and the gateway invokes this app over HTTP rather than running
# ticks itself, so only one process ever enters these jobs. If uvicorn is ever
# given multiple workers, or a second container is allowed to run the jobs, each
# process gets its own lock and the protection silently disappears; the upgrade
# is a lock row in the database, keyed by job name, with a staleness rule so a
# crash mid-run cannot wedge the job forever.
_locks: dict[str, threading.Lock] = {}


def _correlation_id(supplied: str | None) -> str:
    """One id per event, minted at the edge if the caller did not supply one.

    An event now crosses two runtimes (gateway -> plugin -> here -> handler ->
    any resulting send). Without a shared id, a failure halfway is untraceable
    in either log.
    """
    return (supplied or "").strip() or f"int-{uuid.uuid4().hex[:12]}"


def _authorized(request: Request, secret: str | None) -> str | None:
    """Return a rejection reason, or None when the request may proceed."""
    if not config.INTERNAL_API_SECRET:
        # Refuse rather than run open. An unset secret is a misconfiguration,
        # and defaulting to "allow" would make the whole surface public the
        # first time someone forgot the env var.
        return "internal API secret is not configured"
    if (secret or "") != config.INTERNAL_API_SECRET:
        return "bad or missing secret"
    host = request.client.host if request.client else ""
    allowed = config.INTERNAL_API_ALLOW_HOSTS
    if allowed and host not in allowed:
        return f"host {host!r} not in allowlist"
    return None


def _guard(request: Request, secret: str | None, route: str, correlation: str):
    reason = _authorized(request, secret)
    if reason is None:
        return None
    # WARNING, not ERROR: ADR-0015 reserves ERROR for "Justin must act", and a
    # rejected internal call is usually a misconfigured gateway, not his job.
    logger.warning("internal %s rejected (%s) correlation=%s", route, reason, correlation)
    return JSONResponse({"error": "rejected"}, status_code=403)


def run_exclusive(job: str, fn) -> tuple[bool, object]:
    """Run `fn` unless the same job is already running.

    Returns (ran, result). `ran=False` means an invocation is already in flight
    and this one deliberately did nothing.
    """
    lock = _locks.setdefault(job, threading.Lock())
    if not lock.acquire(blocking=False):
        return False, None
    try:
        return True, fn()
    finally:
        lock.release()


def _run(route: str, fn, correlation: str):
    logger.info("internal %s starting correlation=%s", route, correlation)
    try:
        ran, outcome = run_exclusive(route, fn)
    except Exception as exc:
        # Never swallow. The caller is a cron entry with no human watching it,
        # so the only place this can surface is the log.
        level = logging.WARNING if pipeline._is_transient(exc) else logging.ERROR
        logger.log(level, "internal %s failed correlation=%s: %s", route, correlation, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "route": route, "correlation_id": correlation, "reason": str(exc)},
            status_code=500,
        )
    if not ran:
        # Not an error: cron fired while the previous run was still going. Say
        # so explicitly so a skipped run is never read as a run that happened.
        logger.info("internal %s skipped, already running correlation=%s", route, correlation)
        return {"status": "skipped", "route": route, "correlation_id": correlation,
                "reason": "already running"}
    logger.info("internal %s done correlation=%s result=%s", route, correlation, outcome)
    return {"status": "ok", "route": route, "correlation_id": correlation, "result": outcome}


def _endpoint(route: str, fn):
    def handler(
        request: Request,
        x_openclaw_secret: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ):
        correlation = _correlation_id(x_correlation_id)
        rejected = _guard(request, x_openclaw_secret, route, correlation)
        return rejected if rejected is not None else _run(route, fn, correlation)

    return handler


# One shape for all three: guard, correlate, run under the job's lock, report.
router.post("/tick")(_endpoint("tick", lambda: pipeline.run_once()))
router.post("/ingest")(_endpoint("ingest", lambda: gmail_ingest.poll_once()))
router.post("/nudge")(_endpoint("nudge", lambda: pipeline.nudge_stale_actions()))
