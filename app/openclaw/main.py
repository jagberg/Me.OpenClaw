import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import claim_forms, claim_status, db, gmail_ingest, netbank_csv, pipeline, tasks
from .scheduler import scheduler

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()
    gmail_ingest.start_polling()
    pipeline.start()
    yield


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
        needs_pet = conn.execute(
            "SELECT bank_transactions.*, vet_claims.id AS claim_id FROM bank_transactions "
            "JOIN vet_claims ON vet_claims.transaction_id = bank_transactions.id "
            "WHERE bank_transactions.vet_flag = 1 AND vet_claims.pet_id IS NULL"
        ).fetchall()
        pending_match = conn.execute(
            "SELECT vet_claims.*, bank_transactions.date AS txn_date, "
            "bank_transactions.amount AS txn_amount, bank_transactions.merchant AS txn_merchant "
            "FROM vet_claims JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.status = 'pending_match' AND vet_claims.pet_id IS NOT NULL"
        ).fetchall()
        matched = conn.execute(
            "SELECT vet_claims.*, pets.name AS pet_name, bank_transactions.merchant AS txn_merchant, "
            "bank_transactions.amount AS txn_amount FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "LEFT JOIN pets ON pets.id = vet_claims.pet_id WHERE vet_claims.status = 'matched'"
        ).fetchall()
        drafted = conn.execute(
            "SELECT vet_claims.*, pets.name AS pet_name, bank_transactions.merchant AS txn_merchant "
            "FROM vet_claims JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "LEFT JOIN pets ON pets.id = vet_claims.pet_id WHERE vet_claims.status = 'drafted'"
        ).fetchall()
        pets = conn.execute("SELECT * FROM pets").fetchall()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tasks": open_tasks,
            "reminders": due_reminders,
            "needs_pet": needs_pet,
            "pending_match": pending_match,
            "matched": matched,
            "drafted": drafted,
            "pets": pets,
            "upload_error": upload_error,
            **claim_status.dashboard_lists(),
        },
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
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET pet_id = ?, updated_at = ? WHERE id = ?",
            (pet_id, datetime.now(timezone.utc).isoformat(), claim_id),
        )
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/condition")
def set_condition(claim_id: int, condition_text: str = Form(...)):
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET condition_text = ?, updated_at = ? WHERE id = ?",
            (condition_text, datetime.now(timezone.utc).isoformat(), claim_id),
        )
    claim_forms.process_claim(claim_id)
    return RedirectResponse("/", status_code=303)


@app.post("/claims/{claim_id}/sent")
def mark_sent(claim_id: int):
    # A batch submission is several claims sharing one draft — sending that
    # one email sends them all, so one click advances the whole group.
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        claim = conn.execute("SELECT draft_id FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim and claim["draft_id"]:
            conn.execute(
                "UPDATE vet_claims SET status = 'sent', updated_at = ? WHERE draft_id = ? AND status = 'drafted'",
                (now, claim["draft_id"]),
            )
        else:
            conn.execute(
                "UPDATE vet_claims SET status = 'sent', updated_at = ? WHERE id = ? AND status = 'drafted'",
                (now, claim_id),
            )
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
