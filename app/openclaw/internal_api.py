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

import concurrent.futures
import logging
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from . import config, db, gmail_ingest, message_log, pipeline, trace

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


def record_run(route: str, column: str, error: str | None = None) -> None:
    """Stamp `job_runs` with what just happened to this route. Task 5.6.

    Never raises: a liveness record that can kill the job it records is worse than
    no record. It logs at WARNING and returns, because the run itself succeeded
    and the only casualty is the freshness signal.
    """
    assert column in ("last_started_at", "last_ok_at", "last_error_at", "last_skipped_at"), column
    now = datetime.now(timezone.utc).isoformat()
    # WHICH WRITES TOUCH last_error, and why it is not "all of them".
    #
    # First cut cleared it on every write, including `last_started_at` — so the
    # NEXT run's start erased the previous run's error text. Found live 2026-08-04:
    # `/health` reported `last_error_at: 03:12:27` with `last_error: null`, and the
    # container holding the matching log line had since been recreated, so the
    # reason those runs failed is simply gone. A record that deletes the diagnostic
    # it exists to keep is worse than no record.
    #
    # A success clears it (the fault is over, and a stale error next to a fresh
    # `last_ok_at` reads as an outage that is still happening). A start and a skip
    # leave it alone.
    if column == "last_error_at":
        error_sql, error_arg = ", last_error = ?", (error or "")[:300]
    elif column == "last_ok_at":
        error_sql, error_arg = ", last_error = ?", None
    else:
        error_sql, error_arg = "", None
    try:
        with db.get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO job_runs (route) VALUES (?)", (route,))
            params = (now, error_arg, route) if error_sql else (now, route)
            conn.execute(
                f"UPDATE job_runs SET {column} = ?{error_sql} WHERE route = ?", params)
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.warning("could not record the %s run of %s: %s", column, route, exc)


def tee_inbound(correlation: str, text: str, username: str | None, chat_id=None) -> str:
    """Write the inbound row for a gateway-delivered command or typed message.

    Task 4.2, and it closes a gap that was live for a day: nothing wrote an
    inbound row after the cutover, so `telegram_messages` had only outbound rows
    and ADR-0014's "did my tap register?" was answerable solely from container
    logs — which is the dependency ADR-0014 exists to remove.

    **The id is synthetic, and that is a deliberate trade (Justin, 2026-08-04).**
    A plugin command ctx carries no message id and no update id — not under
    another name, they are simply absent from the object the gateway builds — so
    Telegram's own `update_id` is unavailable on this path. The correlation id is
    minted per delivery and is already threaded through every log line and every
    outbound row, so it is the closest thing to an event identity we have. What
    that costs: a Telegram redelivery of the same tap arrives with a NEW
    correlation and therefore writes a SECOND row, where a real `update_id` would
    have deduped. That is an audit-trail wrinkle rather than a money risk — the
    data-layer guards are what stop a double mutation, and they held live on
    2026-08-04 when two Dismiss taps six seconds apart produced exactly one
    `mismatch_dismissed` event.

    Shaped as a Telegram update on purpose. `_describe` is the one classifier for
    both transports, and a `/`-prefixed text already yields kind `command`, so
    these rows carry the same vocabulary the PTB era wrote. No new `kind` value.

    **A suffix, because a correlation id is NOT unique across restarts.** The
    plugin mints `tg-<verb>-n<counter>` from a MODULE-LEVEL counter (`let sequence
    = 0`), which resets every time the plugin reloads — i.e. on every deploy. So
    `tg-actions-n1` recurs exactly, and with `record_inbound_raw`'s
    `INSERT OR IGNORE` on a UNIQUE column a repeat would write NO row for the new
    tap and then let `settle_inbound` stamp the pre-restart row instead. Caught
    while answering "did the synthetic id cause a regression?" — the honest answer
    was yes, one I introduced. The row id therefore carries a random suffix and is
    only an identity; `correlation_id` is the join key, and it lives in its own
    column now (10.14).

    Returns the synthetic id, for the caller to settle.
    """
    inbound_id = f"cmd:{correlation}:{uuid.uuid4().hex[:8]}"
    try:
        message_log.record_inbound_raw(
            inbound_id,
            {"message": {"text": text,
                         "from": {"username": username or ""},
                         "chat": {"id": chat_id if chat_id is not None else db.registered_chat_id()}}},
            correlation=correlation,
        )
    except Exception as exc:  # noqa: BLE001 — a lost log row must not lose the tap
        logger.warning("could not tee inbound %s: %s", inbound_id, exc)
    return inbound_id


def settle_inbound(inbound_id: str, error: str | None = None) -> None:
    """Settle the row this request created, successfully or not.

    **Always settled, never left pending, and that is not laziness.** `pending()`
    is the replay queue, and after the cutover NOTHING DRAINS IT:
    `message_log.replay_pending` rebuilds a python-telegram-bot `Update` and calls
    `application.process_update`, and its only caller is `telegram_bot.py` — off
    since the cutover and deleted by section 6. So a row left unprocessed would
    not "replay later"; it would sit in a queue with no consumer, and `/health`'s
    `queued` count would read as work pending that will never happen. A misleading
    signal is worse than an honest gap.

    Order matters: `mark_processed` refuses a row that already carries an error
    (deliberately — that rule is what kept failed PTB updates in the queue), so
    settle first and annotate second, exactly as the replay path does.
    """
    try:
        message_log.mark_processed(inbound_id)
        if error:
            message_log.mark_failed(inbound_id, error)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not settle inbound %s: %s", inbound_id, exc)


def _run(route: str, fn, correlation: str):
    logger.info("internal %s starting correlation=%s", route, correlation)
    record_run(route, "last_started_at")
    try:
        ran, outcome = run_exclusive(route, fn)
    except Exception as exc:
        # Never swallow. The caller is a cron entry with no human watching it,
        # so the only place this can surface is the log.
        level = logging.WARNING if pipeline._is_transient(exc) else logging.ERROR
        logger.log(level, "internal %s failed correlation=%s: %s", route, correlation, exc, exc_info=True)
        record_run(route, "last_error_at", str(exc))
        return JSONResponse(
            {"status": "error", "route": route, "correlation_id": correlation, "reason": str(exc)},
            status_code=500,
        )
    if not ran:
        # Not an error: cron fired while the previous run was still going. Say
        # so explicitly so a skipped run is never read as a run that happened.
        # Recorded separately from a success for the same reason — a route that
        # only ever skips is a stuck lock, and it must not read as healthy.
        logger.info("internal %s skipped, already running correlation=%s", route, correlation)
        record_run(route, "last_skipped_at")
        return {"status": "skipped", "route": route, "correlation_id": correlation,
                "reason": "already running"}
    logger.info("internal %s done correlation=%s result=%s", route, correlation, outcome)
    record_run(route, "last_ok_at")
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


# One shape for all five: guard, correlate, run under the job's lock, report.
#
# Five, not three, because APScheduler ran five jobs and the cron cutover has to
# reach all of them. The two added 2026-08-04 were previously in-process only, so
# without them the gateway's cron would drive the tick while the weekly vet chase
# and the queue expiry quietly stopped happening the moment the in-process
# scheduler was disabled — a silent loss of the exact kind the hard rules forbid.
#
# The sixth APScheduler job has no endpoint on purpose: reminders are one-shots at
# arbitrary minutes, which cron cannot express, so `reminders.sweep_due()` runs
# inside `pipeline.run_once` and rides the tick.
router.post("/tick")(_endpoint("tick", lambda: pipeline.run_once()))
router.post("/ingest")(_endpoint("ingest", lambda: gmail_ingest.poll_once()))
router.post("/nudge")(_endpoint("nudge", lambda: pipeline.nudge_stale_actions()))
router.post("/vet-nudge")(_endpoint("vet-nudge", lambda: pipeline.nudge_unanswered_vet_requests()))
router.post("/expire-queue")(_endpoint("expire-queue", lambda: message_log.expire_queue()))


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


# How long each route may go unrun before it is overdue, in minutes. Derived from
# the same config the schedules are, so changing an interval cannot leave the
# check asserting the old one.
#
# The multiplier is 3 on the frequent jobs and a day of slack on the calendar
# ones, deliberately loose: this must fire on "nothing is driving this at all",
# not on a stagger window or one skipped run. A tripwire that cries during normal
# operation gets ignored, and then it is not a tripwire.
def _expected_max_idle() -> dict[str, int]:
    day = 24 * 60
    return {
        "tick": config.VET_CLAIM_PIPELINE_INTERVAL_MINUTES * 3,
        "ingest": config.GMAIL_POLL_INTERVAL_MINUTES * 3,
        "nudge": day + 6 * 60,
        "vet-nudge": 7 * day + day,
        "expire-queue": day + 6 * 60,
    }


def scheduler_health() -> dict:
    """Which runtime is scheduling, and whether anything has actually driven each
    job lately. Task 5.6.

    The failure being made visible: after the cron cutover the app no longer has
    any opinion about *when* work happens, so a cron entry that was never declared
    or was silently disabled produces exactly what a quiet week produces. This
    reports the last outcome per route and names the overdue ones.

    `owner` is read from the flag rather than inferred, because "nothing has run"
    means two different things: with the in-process scheduler ON it is a broken
    app; with it OFF it is a missing cron entry, and those have different fixes.

    Reports a read failure as a value. `/health` is the URL consulted when
    something is already wrong, and this must never be the reason it 500s — the
    projection count learned that the hard way (see main.health).
    """
    owner = "in-process scheduler" if config.SCHEDULER_ENABLED else "gateway cron"
    try:
        with db.get_connection() as conn:
            rows = {r["route"]: r for r in conn.execute(
                "SELECT route, last_started_at, last_ok_at, last_error_at, last_error, "
                "last_skipped_at FROM job_runs").fetchall()}
    except Exception as exc:  # noqa: BLE001 — see docstring
        return {"owner": owner, "error": f"could not read job_runs: {exc}"}

    now = datetime.now(timezone.utc)
    jobs, overdue = {}, []
    for route, max_idle in _expected_max_idle().items():
        row = rows.get(route)
        last_ok = row["last_ok_at"] if row else None
        entry = {"last_ok_at": last_ok, "max_idle_minutes": max_idle}
        if row and row["last_error_at"]:
            entry["last_error_at"] = row["last_error_at"]
            entry["last_error"] = row["last_error"]
        if row and row["last_skipped_at"]:
            entry["last_skipped_at"] = row["last_skipped_at"]

        if not last_ok:
            # No row at all is the state a never-declared cron entry leaves, and
            # it is the one this whole mechanism exists to catch. A fresh DB looks
            # identical for one cadence, which is why the value says "never" rather
            # than claiming an age it cannot know.
            entry["minutes_since_ok"] = None
            overdue.append(f"{route}: never")
        else:
            try:
                when = datetime.fromisoformat(last_ok)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                idle = int((now - when).total_seconds() // 60)
            except ValueError:
                entry["minutes_since_ok"] = None
                overdue.append(f"{route}: unreadable timestamp")
            else:
                entry["minutes_since_ok"] = idle
                if idle > max_idle:
                    overdue.append(f"{route}: {idle}m > {max_idle}m")
        jobs[route] = entry

    return {"owner": owner, "jobs": jobs, "overdue": overdue}


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
    args = body.get("args") or ""
    # BEFORE dispatch, so the row exists even if the handler dies (task 4.2).
    inbound_id = tee_inbound(correlation, f"/{name} {args}".strip(), username)
    try:
        with trace.step("command.dispatch", correlation, command=name):
            outcome = commands.dispatch(name, args, username)
    except Exception as exc:  # noqa: BLE001 — a tap that failed must say so
        settle_inbound(inbound_id, str(exc))
        logger.error("command %s failed correlation=%s: %s", name, correlation, exc, exc_info=True)
        return JSONResponse({"status": "error", "route": f"command/{name}",
                             "correlation_id": correlation, "result": f"/{name} failed: {exc}"},
                            status_code=500)

    sent, failed = 0, []
    target = db.registered_chat_id()

    def deliver(index_card: tuple[int, dict]) -> str | None:
        index, card = index_card
        try:
            with trace.step("command.deliver", correlation, card=index,
                            kind="png" if card.get("png") is not None else "text"):
                _deliver_one(card)
            return None
        except Exception as exc:  # noqa: BLE001
            # Never silent: a card that did not arrive must not read as one that did.
            logger.error("card delivery failed for /%s correlation=%s: %s", name, correlation, exc)
            return str(exc)

    def _deliver_one(card: dict) -> None:
        if card.get("png") is not None:
            gateway_client.send_card(str(target), card["png"], caption=card.get("caption", ""),
                                     buttons=card.get("buttons") or None)
        else:
            gateway_client.send_message(str(target), card["text"],
                                        buttons=card.get("buttons") or None)

    cards = outcome["cards"]
    if target is None:
        failed.append("no registered chat")
    elif cards and gateway_client.using_http_route():
        # The fast path: ONE local HTTP call, N in-process dispatches, order
        # preserved. See `gateway_client.send_cards` for why the CLI burst it
        # replaces could not get below ~9s per message.
        try:
            with trace.step("command.route_send", correlation, cards=len(cards)):
                gateway_client.send_cards(str(target), cards, correlation=correlation)
            sent = len(cards)
        except Exception as exc:  # noqa: BLE001
            logger.error("in-gateway send failed for /%s correlation=%s: %s", name, correlation, exc)
            failed.append(str(exc))
    elif cards:
        # **All at once, including the rendered summary card.** Every send costs
        # 9-13s end to end, and the decomposition (see `trace`) is ~6.6s of local
        # CLI initialisation, ~2.5s of connect + auth, and under a second of
        # gateway work -- so ordering two rounds doubles the wall time for
        # nothing. Nothing here is order-dependent; the summary is one card in
        # the burst and may not arrive first.
        #
        # Concurrency measured sub-linear (5 sends: ~32s serial, ~15s wall), so
        # the pool is capped rather than unbounded -- the gateway serialises
        # some of it and more threads buy less each.
        with trace.step("command.burst", correlation, cards=len(cards)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(cards))) as pool:
                outcomes = list(pool.map(deliver, enumerate(cards)))
        failed.extend(o for o in outcomes if o is not None)
        sent = sum(1 for o in outcomes if o is None)

    text = outcome["text"]
    if failed:
        text = (text + f"\n⚠️ {len(failed)} card(s) did not send: {failed[0]}").strip()
    elif sent and not text:
        text = f"Sent {sent} card(s)."
    logger.info("command /%s cards=%s failed=%s correlation=%s", name, sent, len(failed), correlation)
    # The command ran either way; a card that did not arrive is annotated on the
    # row rather than hidden, so "it registered" and "you saw the answer" stay
    # distinguishable in the dataset.
    settle_inbound(inbound_id, f"{len(failed)} card(s) did not send: {failed[0]}" if failed else None)
    return {"status": "ok" if not failed else "partial", "route": f"command/{name}",
            "correlation_id": correlation, "result": text}


@router.post("/telegram/ack")
async def telegram_ack(
    request: Request,
    x_openclaw_secret: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """React to a TYPED inbound message so a slow answer does not feel dead.

    Posted by the plugin's `message_received` hook, which the gateway emits
    inside `dispatch-from-config` with the message id in its context.

    **A tapped button never reaches here, and cannot.** Commands are routed
    before dispatch, so no message hook sees them, and the context a plugin
    command handler is given (`commands-CDhgE9eG.js`) contains no message id at
    all — there is nothing to react to. Settled 2026-08-03 by reading that
    construction; two earlier attempts moved the hook around instead, on the
    assumption that some hook must carry it. A tap's feedback is its reply.

    Never fails a caller: `notify.ack` returns a bool. Losing the ack is
    strictly better than delaying or breaking the real handler.
    """
    from . import notify

    correlation = _correlation_id(x_correlation_id)
    rejected = _guard(request, x_openclaw_secret, "telegram/ack", correlation)
    if rejected is not None:
        return rejected
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not commands_is_authorized(body.get("username")):
        return {"status": "ok", "route": "telegram/ack", "correlation_id": correlation,
                "acked": False, "reason": "unauthorized"}
    acked = notify.ack(body.get("message_id"), chat_id=body.get("chat_id"))
    return {"status": "ok", "route": "telegram/ack", "correlation_id": correlation, "acked": acked}


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
    # No ack here: `message_received` fires earlier and covers every inbound
    # message including commands, so acking in both places would react twice.
    #
    # But DO log it. `before_dispatch` runs after command routing, so this route
    # sees exactly the typed messages the command route does not — the two tees
    # together cover the inbound side without double-writing either kind.
    inbound_id = tee_inbound(correlation, text, username, chat_id=chat_id)
    try:
        card = pending_flows.claim_text(chat_id, text)
    except Exception as exc:  # noqa: BLE001 — fail open, loudly
        settle_inbound(inbound_id, str(exc))
        logger.error("pending-flow claim check failed correlation=%s: %s", correlation, exc, exc_info=True)
        return {"status": "error", "route": "telegram/claim", "correlation_id": correlation,
                "claimed": False, "reason": str(exc)}

    settle_inbound(inbound_id)
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
        recorded = message_log.record_inbound_raw(update_id, raw, correlation=correlation)
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
