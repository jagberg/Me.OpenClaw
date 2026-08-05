import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import (
    claim_forms,
    claim_status,
    config,
    db,
    gmail_ingest,
    internal_api,
    mcp_server,
    message_log,
    netbank_csv,
    pipeline,
    status_labels,
    tasks,
    telegram_bot,
)
from .scheduler import scheduler

# Without this the root level is WARNING and every logger.info in the app is
# discarded — a whole morning of bot activity left no trace anywhere.
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs the full request URL at INFO, and the Telegram API embeds the bot
# token in the path — that would write the token into every container log line.
# Secrets never go to logs (root CLAUDE.md), so this stays at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# One vocabulary for both templates, the Telegram cards and the notify text —
# the three per-template label maps this replaces had to be kept in sync by hand.
templates.env.globals["status_label"] = status_labels.label
templates.env.globals["status_needs"] = status_labels.needs
# For the rows that have no claim to pass to `status_label` — a vet charge with no
# claim row yet still has to say "No invoice", and saying it literally is how the
# three hand-synced maps started (ADR-0021).
templates.env.globals["status_words"] = status_labels.LABELS
# Event details are stored as JSON text; the review queue renders the figures a
# dismissed assessment difference was made of.
templates.env.filters["from_json"] = lambda raw: json.loads(raw or "{}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Every logged message is stamped with this, so a wrong version silently
    # mislabels the training data — say so rather than shipping "unknown".
    if config.APP_VERSION == "unknown":
        logger.warning(
            "APP_VERSION is 'unknown' — built without scripts/deploy.ps1; messages will be mistagged."
        )
    else:
        logger.info("OpenClaw starting, version %s", config.APP_VERSION)
    db.init_db()
    # The gateway's cron drives `/internal/*` once this is off (task 5.1). Both
    # schedulers running would fire the daily nudge twice — `run_exclusive` only
    # dedupes *concurrent* runs, and two schedulers ten seconds apart are not
    # concurrent. So this is a swap, not an overlap, and the flag exists to make
    # it reversible in one env var + restart, the same shape as the Telegram
    # cutover (4.1). Section 6 deletes both halves.
    if config.SCHEDULER_ENABLED:
        scheduler.start()
        gmail_ingest.start_polling()
        pipeline.start()
    else:
        # Not a warning. This is the intended post-cutover state, and an ERROR
        # here would train Justin to ignore the log. What IS a failure is nobody
        # driving the endpoints, and that is `stale_tick_minutes` on /health.
        logger.info("in-process scheduler disabled — the gateway's cron owns scheduling")
    await telegram_bot.start_polling()
    yield
    await telegram_bot.stop_polling()


app = FastAPI(lifespan=lifespan)
# The gateway's entry point into this app: cron invocations and (later) inbound
# channel events. Secret-guarded, loopback by default — see internal_api.
app.include_router(internal_api.router)
# The other half of the split: deterministic work arrives at /internal, and a
# typed question arrives here as MCP tool calls. Read only, same shared secret —
# the answers name pets, vets and amounts, so an open endpoint leaks the history.
app.include_router(mcp_server.router)


@app.get("/")
def dashboard(request: Request, upload_error: str | None = None):
    with db.get_connection() as conn:
        open_tasks = conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' ORDER BY created_at DESC"
        ).fetchall()
        due_reminders = conn.execute(
            "SELECT reminders.*, tasks.description AS task_description FROM reminders "
            "JOIN tasks ON tasks.id = reminders.task_id WHERE reminders.status = 'due' "
            "ORDER BY reminders.scheduled_at"
        ).fetchall()
        pets = conn.execute("SELECT * FROM pets").fetchall()

    # One transaction-anchored ledger replaces the old needs_pet / pending_match /
    # matched / drafted parallel lists — every vet charge appears once, claims
    # nested beneath (see change unified-visit-claim-view).
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tasks": open_tasks,
            "reminders": due_reminders,
            "pets": pets,
            "ledger": claim_status.visit_ledger(),
            "upload_error": upload_error,
            **claim_status.dashboard_lists(),
        },
    )


@app.get("/basic")
def basic_status(request: Request):
    """Stripped-down, phone-first view: outstanding visits + recently closed, as
    stacked cards (no wide table). Derived from the same visit_ledger()."""
    ledger = claim_status.visit_ledger()
    outstanding, closed = [], []
    for entry in ledger:
        for claim in entry["claims"]:
            row = {"txn": entry["txn"], "claim": claim}
            if claim["status"] in claim_status.CLOSED_STATUSES:
                closed.append(row)
            else:
                outstanding.append(row)
    closed.sort(key=lambda r: r["claim"]["status"] != "settled")  # settled (has money) first
    return templates.TemplateResponse(
        "basic.html", {"request": request, "outstanding": outstanding, "closed": closed}
    )


def _disagreement_count():
    """The shadow count, or a visible marker if the fold itself is broken.

    Never a number when it failed: reporting 0 for "could not compute" would
    read as healthy, and this figure is what gates Phase 2."""
    try:
        return len(claim_status.state_projection_disagreements())
    except Exception as exc:
        logger.warning("health: state projection failed: %s", exc, exc_info=True)
        return f"unavailable: {type(exc).__name__}"


@app.get("/health")
def health():
    """One URL that answers "is the bot actually listening?". Probing Telegram's
    getUpdates from outside can't tell — it races the gap between long polls."""
    return {
        "app_version": config.APP_VERSION,
        "polling_alive": telegram_bot.polling_alive(),
        # Shadow mode (Phase 1): claims whose stored status differs from what
        # their own event log projects. `pipeline.compare_state_projection`
        # guards the identical call and this one did not, so a malformed detail
        # in the fold turned the whole endpoint into a 500 — taking
        # `polling_alive` down with it, on the one URL you check to find out
        # whether anything is wrong. Reports the failure as a value instead.
        "state_projection_disagreements": _disagreement_count(),
        # What the in-gateway plugin reported registering, this boot. Empty
        # means it has not run — which is a broken tap path, not a quiet one:
        # an unregistered command in a button reaches the model instead of
        # erroring. `scripts/gateway_preflight.py` fails the deploy on it.
        "gateway_plugin": internal_api.plugin_report(),
        # Recorded separately and never as `app_version`. Two runtimes mean two
        # versions, and `telegram_messages.app_version` exists so the dataset is
        # keyed to the code that produced a row — conflating them makes it lie.
        "gateway_version": config.GATEWAY_VERSION or None,
        # Task 5.6, and the whole reason `job_runs` exists. A cron entry that was
        # never declared, was disabled, or whose curl silently fails produces the
        # same empty dashboard as a genuinely quiet week. This says which:
        # `overdue` lists the jobs whose expected cadence has lapsed, so a dead
        # scheduler is a value on the URL Justin already checks rather than an
        # absence he has to notice.
        "scheduler": internal_api.scheduler_health(),
        **message_log.stats(),
    }


@app.get("/messages.jsonl")
def messages_jsonl():
    """The full in/out message stream as JSONL for reinforcement learning, one
    object per line, oldest first, each stamped with the deploy that handled it."""
    return StreamingResponse(message_log.iter_jsonl(), media_type="application/x-ndjson")


@app.post("/tasks")
def capture_task(description: str = Form(...)):
    tasks.create_task(description, source="chat")
    return RedirectResponse("/", status_code=303)


@app.post("/tasks/{task_id}/outcome")
def outcome(task_id: int, outcome: str = Form(...)):
    tasks.record_outcome(task_id, outcome)
    return RedirectResponse("/", status_code=303)


@app.post("/transactions/upload")
async def upload_transactions(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8", errors="replace")
    try:
        rows = netbank_csv.parse(content)
    except netbank_csv.CsvParseError as exc:
        logger.error("NetBank CSV parse failure: %s", exc)
        return RedirectResponse(f"/?upload_error={quote(str(exc))}", status_code=303)

    netbank_csv.import_rows(rows)
    pipeline.run_once()
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/pet")
def assign_pet(claim_id: int, pet_id: int = Form(...)):
    claim_forms.assign_pet(claim_id, pet_id)
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/condition")
def set_condition(claim_id: int, condition_text: str = Form(...)):
    claim_forms.set_condition_text(claim_id, condition_text)
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/sent")
def mark_sent(claim_id: int):
    claim_status.mark_sent(claim_id)
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/confirm-resolved")
def confirm_resolved(claim_id: int):
    claim_status.confirm_resolved(claim_id)
    return RedirectResponse("/", status_code=303)


@app.post("/events/{event_id}/link")
def link_event(event_id: int, claim_id: int = Form(...)):
    claim_status.link_event(event_id, claim_id)
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/invoice-request-sent")
def mark_invoice_request_sent(claim_id: int):
    claim_status.mark_invoice_request_sent(claim_id)
    return RedirectResponse("/", status_code=303)
