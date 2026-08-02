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

from . import config, db, gmail_ingest, message_log, pipeline

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
    """The plugin says, at boot, which commands it ATTEMPTED to register.

    **Not what `registerCommand` accepted** — the eval on 2026-08-02 caught that
    overclaim here. The plugin pushes each name unconditionally
    (`gateway-plugin/index.js`), because `registerCommand` returns nothing and
    reports a collision asynchronously about a second later. So a name that was
    silently refused produces an identical report.

    What this therefore proves: the plugin LOADED AND RAN. That is worth having,
    since both enablement gates fail silently and a plugin that never ran never
    reports. What it does not prove is ownership; `scripts/gateway_preflight.py`
    covers that separately by reading the gateway's log for registration
    failures. Do not restore the stronger wording without making the claim true.
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


def confirm_proposal(args) -> dict:
    """A Confirm tap on a chat-initiated proposal: `/confirm <id>`.

    **Deliberately not its own route.** The plugin forwards every command to
    `/internal/command/<name>`, which section 4 builds; that dispatcher calls
    this. A second door straight to the commit is exactly what ADR-0027 just
    finished collapsing, and one entry point per origin is the property worth
    keeping.

    `args` arrives as the raw string the plugin passed through, so the parse
    lives here rather than in the caller. `proposals.commit` refuses a second
    tap — Telegram redelivers, and a double mark-sent is a second Petcover
    submission for one set of invoices.
    """
    from . import proposals

    try:
        pid = int(str(args).strip())
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Not a proposal id: {args!r}. Nothing was changed."}
    outcome = proposals.commit(pid)
    logger.info("confirm proposal=%s ok=%s", pid, outcome["ok"])
    return outcome


@router.post("/command/{name}")
async def command(
    name: str,
    request: Request,
    x_openclaw_secret: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Every slash command the plugin registered arrives here.

    The plugin holds no claims logic: it forwards `{args}` and returns whatever
    text comes back. Cards are *not* returned — a command's reply is one string
    through the gateway, and a card is an image with its own buttons, so cards
    are pushed separately over `gateway_client` and the reply says what went.

    Authorization stays on this side (task 4.8). The gateway deciding to deliver
    an event is not the same as this app accepting it, and the username check is
    the only thing standing between a stranger's `/mark 7 sent` and a Petcover
    submission.
    """
    from . import commands, gateway_client

    correlation = _correlation_id(x_correlation_id)
    rejected = _guard(request, x_openclaw_secret, f"command/{name}", correlation)
    if rejected is not None:
        return rejected
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    username = body.get("username") or config.TELEGRAM_USERNAME
    try:
        outcome = commands.dispatch(name, body.get("args") or "", username)
    except Exception as exc:  # noqa: BLE001 — a tap that failed must say so
        logger.error("command %s failed correlation=%s: %s", name, correlation, exc, exc_info=True)
        return JSONResponse({"status": "error", "route": f"command/{name}",
                             "correlation_id": correlation, "result": f"/{name} failed: {exc}"},
                            status_code=500)

    sent, failed = 0, []
    target = db.registered_chat_id()
    for card in outcome["cards"]:
        if target is None:
            failed.append("no registered chat")
            break
        try:
            if card.get("png") is not None:
                gateway_client.send_card(str(target), card["png"], caption=card.get("caption", ""),
                                         buttons=card.get("buttons") or None)
            else:
                gateway_client.send_message(str(target), card["text"],
                                            buttons=card.get("buttons") or None)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            # Never silent: a card that did not arrive must not read as one that did.
            logger.error("card delivery failed for /%s correlation=%s: %s", name, correlation, exc)
            failed.append(str(exc))

    text = outcome["text"]
    if failed:
        text = (text + f"\n⚠️ {len(failed)} card(s) did not send: {failed[0]}").strip()
    elif sent and not text:
        text = f"Sent {sent} card(s)."
    logger.info("command /%s cards=%s failed=%s correlation=%s", name, sent, len(failed), correlation)
    return {"status": "ok" if not failed else "partial", "route": f"command/{name}",
            "correlation_id": correlation, "result": text}


@router.post("/telegram/claim")
async def telegram_claim(
    request: Request,
    x_openclaw_secret: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Does a pending flow own this message? The plugin asks; the app decides.

    Reached from the plugin's `before_dispatch` hook, which runs after command
    routing and before the model. A `claimed: true` answer means the plugin
    returns `{ handled: true }` and the agent never sees the text — which is
    the whole point: `condition_text` is a field the hard rules forbid
    inferring, and the chat agent would cheerfully interpret the words.

    The decision lives in Python, next to the data. A plugin that decided for
    itself would be a second copy of "is a flow pending", and this codebase has
    been bitten five times by second copies.

    **Fails open, deliberately.** If this errors the answer is "not claimed",
    so the message reaches the agent rather than vanishing. A lost message is
    worse than an unnecessary chat turn — but the failure is logged at ERROR,
    because a flow that stopped claiming is a condition entry silently going to
    a model.
    """
    from . import pending_flows

    correlation = _correlation_id(x_correlation_id)
    rejected = _guard(request, x_openclaw_secret, "telegram/claim", correlation)
    if rejected is not None:
        return rejected
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    username, text = body.get("username"), body.get("text") or ""
    if not commands_is_authorized(username):
        # Not this app's user. Claim nothing and say so — the gateway can do
        # what it likes with a stranger's message, but no flow of Justin's may
        # consume it.
        return {"status": "ok", "route": "telegram/claim", "correlation_id": correlation,
                "claimed": False, "reason": "unauthorized"}

    chat_id = body.get("chat_id") or db.registered_chat_id()
    try:
        card = pending_flows.claim_text(chat_id, text)
    except Exception as exc:  # noqa: BLE001 — fail open, loudly
        logger.error("pending-flow claim check failed correlation=%s: %s", correlation, exc, exc_info=True)
        return {"status": "error", "route": "telegram/claim", "correlation_id": correlation,
                "claimed": False, "reason": str(exc)}

    if card is None:
        return {"status": "ok", "route": "telegram/claim", "correlation_id": correlation,
                "claimed": False}
    logger.info("pending flow claimed a message correlation=%s", correlation)
    return {"status": "ok", "route": "telegram/claim", "correlation_id": correlation,
            "claimed": True, "reply": card.get("text") or card.get("prompt") or "",
            "buttons": card.get("buttons") or []}


def commands_is_authorized(username: str | None) -> bool:
    from . import commands

    return commands.is_authorized(username)


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
