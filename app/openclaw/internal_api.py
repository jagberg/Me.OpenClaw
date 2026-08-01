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
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from . import config, gmail_ingest, message_log, pipeline

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


# What the in-gateway plugin reported it registered, this boot.
#
# **In-memory on purpose.** The one thing this must describe is the plugin that
# is running right now. Persisting it would recreate precisely the failure that
# makes `plugins list` useless — it reads a saved registry that goes stale
# silently and reported `commands: []` for commands that demonstrably worked
# (18.6). A restart of either runtime must empty this, so an absent report reads
# as "the plugin has not run", which is the state the deploy has to catch.
#
# Why it exists at all: an unregistered command in a button is not an error. It
# reaches the agent as a chat turn and spends tokens — measured live, three
# times, in Justin's chat (16.8). Both of a plugin's enablement gates fail
# silently (18.7), so "it loaded" is not evidence that it ran.
_plugin_report: dict = {}


@router.post("/plugin/hello")
async def plugin_hello(
    request: Request,
    x_openclaw_secret: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """The plugin says, at boot, what `api.registerCommand` actually accepted.

    Self-reported, and that limit is stated rather than hidden: this is a
    runtime signal from inside the registration call, which is stronger than
    reading a registry and weaker than a real tap. A tap cannot be faked by a
    deploy script against Justin's chat, so this is the best available.
    """
    correlation = _correlation_id(x_correlation_id)
    rejected = _guard(request, x_openclaw_secret, "plugin/hello", correlation)
    if rejected is not None:
        return rejected
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    commands = sorted({str(c).lstrip("/") for c in (body.get("commands") or [])})
    _plugin_report.clear()
    _plugin_report.update({
        "plugin": body.get("plugin") or "unknown",
        "version": body.get("version"),
        "commands": commands,
        "reported_at": datetime.now(timezone.utc).isoformat(),
    })
    # INFO, not DEBUG: this line is how you tell a plugin that ran from one that
    # loaded and did nothing, and the difference is a whole broken tap path.
    logger.info("gateway plugin %s registered %s correlation=%s",
                _plugin_report["plugin"], commands or "NOTHING", correlation)
    return {"status": "ok", "route": "plugin/hello", "correlation_id": correlation,
            "commands": commands}


def plugin_report() -> dict:
    """For `/health`. Empty means the plugin has not reported since this boot."""
    return dict(_plugin_report)


@router.post("/telegram/event")
async def telegram_event(
    request: Request,
    x_openclaw_secret: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """The logging tee. A copy of every inbound message, for the log only.

    **This is not a bridge, and the distinction is the whole design.** The
    gateway's agent is the message processor; nothing here dispatches a handler,
    and the app does not act on what arrives. What it does is keep
    `telegram_messages` complete — otherwise the training dataset Justin kept
    the table for (ADR-0014, and his explicit "hard keep" on 2026-08-01) narrows
    to callbacks and outbound, which is the half he did not ask for.

    It deliberately does NOT use `run_exclusive`: a write-and-return has no
    overlap hazard, and serialising the tee behind a lock would let a slow
    write hold up the gateway's delivery loop.

    Failure posture: a tee that cannot write must say so and must not pretend.
    It returns 500 rather than swallowing, because a silently missing row is
    indistinguishable from a message that never arrived — which is the exact
    ambiguity the table exists to remove.
    """
    correlation = _correlation_id(x_correlation_id)
    rejected = _guard(request, x_openclaw_secret, "telegram/event", correlation)
    if rejected is not None:
        return rejected
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is the caller's bug, and it is not ours to guess at
        logger.warning("internal telegram/event got an unparseable body correlation=%s", correlation)
        return JSONResponse({"error": "invalid json"}, status_code=400)
    return record_event(body, correlation)


def record_event(body: dict, correlation: str):
    """The tee's whole body of work, separated from the HTTP plumbing.

    ponytail: split out so the suite can exercise it directly. The rest of this
    module's tests call functions rather than routes for the same reason —
    fastapi's TestClient pulls in httpx, and a test-only dependency to reach
    code that is already callable buys nothing.
    """
    update_id = body.get("update_id")
    raw = body.get("update") or {}
    if update_id is None:
        # Without an id there is no dedupe key and no way to settle the row, so
        # the log would silently accumulate duplicates on every redelivery.
        logger.warning("internal telegram/event has no update_id correlation=%s", correlation)
        return JSONResponse({"error": "update_id required"}, status_code=400)

    logger.info("internal telegram/event starting correlation=%s update_id=%s", correlation, update_id)
    try:
        recorded = message_log.record_inbound_raw(update_id, raw)
    except Exception as exc:
        logger.error("internal telegram/event failed correlation=%s: %s", correlation, exc, exc_info=True)
        return JSONResponse(
            {"status": "error", "route": "telegram/event", "correlation_id": correlation,
             "reason": str(exc)},
            status_code=500,
        )
    # `duplicate` is not an error — Telegram redelivers, and the gateway's own
    # spool may replay. Say which happened rather than reporting a bare ok, so a
    # redelivery storm is visible in the log instead of looking like traffic.
    status = "ok" if recorded is not None else "duplicate"
    logger.info("internal telegram/event %s correlation=%s update_id=%s", status, correlation, update_id)
    return {"status": status, "route": "telegram/event", "correlation_id": correlation,
            "update_id": update_id}
