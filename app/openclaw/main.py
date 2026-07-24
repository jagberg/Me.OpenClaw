import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import claim_forms, claim_status, db, gmail_ingest, netbank_csv, pipeline, tasks, telegram_bot
from .scheduler import scheduler

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()
    gmail_ingest.start_polling()
    pipeline.start()
    await telegram_bot.start_polling()
    yield
    await telegram_bot.stop_polling()


app = FastAPI(lifespan=lifespan)


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


# Statuses with nothing left for Justin to do — settled/declined are already
# terminal thread-wide (claim_status.TERMINAL_STATUSES); below_excess/absorbed
# are equally closed but don't end a Condition Thread, so they aren't in that
# set. A closed claim must never show up looking like it still needs action.
_CLOSED_STATUSES = claim_status.TERMINAL_STATUSES + ("below_excess", "absorbed")


@app.get("/basic")
def basic_status(request: Request):
    """Stripped-down, phone-first view: outstanding visits + recently closed, as
    stacked cards (no wide table). Derived from the same visit_ledger()."""
    ledger = claim_status.visit_ledger()
    outstanding, closed = [], []
    for entry in ledger:
        for claim in entry["claims"]:
            row = {"txn": entry["txn"], "claim": claim}
            if claim["status"] in _CLOSED_STATUSES:
                closed.append(row)
            else:
                outstanding.append(row)
    closed.sort(key=lambda r: r["claim"]["status"] != "settled")  # settled (has money) first
    return templates.TemplateResponse(
        "basic.html", {"request": request, "outstanding": outstanding, "closed": closed}
    )


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
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET invoice_request_sent_at = ?, flag = NULL, updated_at = ? WHERE id = ?",
            (now, now, claim_id),
        )
    return RedirectResponse("/", status_code=303)
