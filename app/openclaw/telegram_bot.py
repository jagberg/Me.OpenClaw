import asyncio
import logging
from datetime import datetime, timezone

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import claim_forms, claim_status, config, db

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "/mark <claim_id> <condition text> — set the condition being claimed for\n"
    "/mark <claim_id> reviewed — confirm a drafted claim looks right (does not send it)\n"
    "/pet <claim_id> <pet name> — assign a pet to a vet-flagged transaction\n"
    "/process <claim_id> — run the matched→drafted advance now\n"
    "/sent <claim_id> — mark a drafted claim as sent (starts Petcover reply tracking)\n"
    "/resolved <claim_id> — confirm you've dealt with an info request/suspension\n"
    "/vetemail <merchant name> <email> — set a vet's contact address for invoice requests"
)

_application: Application | None = None


def _is_authorized(username: str | None) -> bool:
    return bool(username) and username == config.TELEGRAM_USERNAME


def get_registered_chat_id() -> int | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT chat_id FROM telegram_registrations WHERE username = ?",
            (config.TELEGRAM_USERNAME,),
        ).fetchone()
    return row["chat_id"] if row else None


def register_chat(username: str, chat_id: int) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO telegram_registrations (username, chat_id, registered_at) VALUES (?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET chat_id = excluded.chat_id, registered_at = excluded.registered_at",
            (username, chat_id, datetime.now(timezone.utc).isoformat()),
        )


# Pure command logic, independent of the telegram library — testable without
# constructing an Update, and the single place command handlers delegate to.


def handle_start(username: str | None, chat_id: int) -> str:
    if not _is_authorized(username):
        return ""
    register_chat(username, chat_id)
    return "Registered — you'll get claim notifications here."


def handle_mark(username: str | None, claim_id: int, rest: str) -> dict:
    if not _is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    if rest.strip().lower() == "reviewed":
        return claim_forms.mark_reviewed(claim_id)
    return claim_forms.set_condition_text(claim_id, rest)


def handle_pet(username: str | None, claim_id: int, pet_name: str) -> dict:
    if not _is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    with db.get_connection() as conn:
        pet = conn.execute("SELECT * FROM pets WHERE name = ?", (pet_name,)).fetchone()
    if pet is None:
        return {"ok": False, "message": f"No pet named '{pet_name}'."}
    return claim_forms.assign_pet(claim_id, pet["id"])


def handle_process(username: str | None, claim_id: int) -> dict:
    if not _is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return claim_forms.process_and_report(claim_id)


def handle_sent(username: str | None, claim_id: int) -> dict:
    if not _is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return claim_status.mark_sent(claim_id)


def handle_resolved(username: str | None, claim_id: int) -> dict:
    if not _is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    with db.get_connection() as conn:
        claim = conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None:
        return {"ok": False, "message": f"No claim #{claim_id} found."}
    claim_status.confirm_resolved(claim_id)
    return {"ok": True, "message": f"Claim #{claim_id} confirmed resolved."}


def handle_vetemail(username: str | None, merchant: str, email: str) -> dict:
    if not _is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    if "@" not in email:
        return {"ok": False, "message": f"'{email}' doesn't look like an email address."}
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO vet_contacts (merchant, email) VALUES (?, ?) "
            "ON CONFLICT(merchant) DO UPDATE SET email = excluded.email",
            (merchant, email),
        )
    return {"ok": True, "message": f"Vet contact saved: {merchant} → {email}"}


# Thin async adapters — extract args from the Update/Context, call the pure
# handler above, reply with its message. No business logic lives here.


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = handle_start(user.username if user else None, update.effective_chat.id)
    if text:
        await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if _is_authorized(user.username if user else None):
        await update.message.reply_text(HELP_TEXT)


async def mark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.effective_user.username if update.effective_user else None
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /mark <claim_id> <condition text|reviewed>")
        return
    try:
        claim_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("claim_id must be a number.")
        return
    result = handle_mark(username, claim_id, " ".join(context.args[1:]))
    await update.message.reply_text(result["message"])


async def pet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.effective_user.username if update.effective_user else None
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /pet <claim_id> <pet name>")
        return
    try:
        claim_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("claim_id must be a number.")
        return
    result = handle_pet(username, claim_id, " ".join(context.args[1:]))
    await update.message.reply_text(result["message"])


async def process_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.effective_user.username if update.effective_user else None
    if not context.args:
        await update.message.reply_text("Usage: /process <claim_id>")
        return
    try:
        claim_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("claim_id must be a number.")
        return
    result = handle_process(username, claim_id)
    await update.message.reply_text(result["message"])


def _single_claim_id_command(handler):
    """Adapter factory for commands whose only argument is a claim id —
    /process, /sent, /resolved all share this exact shape."""

    async def command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        username = update.effective_user.username if update.effective_user else None
        if not context.args:
            await update.message.reply_text(f"Usage: /{command.__name__} <claim_id>")
            return
        try:
            claim_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("claim_id must be a number.")
            return
        result = handler(username, claim_id)
        await update.message.reply_text(result["message"])

    return command


sent_command = _single_claim_id_command(handle_sent)
sent_command.__name__ = "sent"
resolved_command = _single_claim_id_command(handle_resolved)
resolved_command.__name__ = "resolved"


async def vetemail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.effective_user.username if update.effective_user else None
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /vetemail <merchant name> <email>")
        return
    # Email is the last token; the merchant name is everything before it
    # (NetBank merchant strings contain spaces, e.g. "CITY VET CLINIC SYDNEY").
    merchant = " ".join(context.args[:-1])
    result = handle_vetemail(username, merchant, context.args[-1])
    await update.message.reply_text(result["message"])


def build_application() -> Application:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mark", mark_command))
    application.add_handler(CommandHandler("pet", pet_command))
    application.add_handler(CommandHandler("process", process_command))
    application.add_handler(CommandHandler("sent", sent_command))
    application.add_handler(CommandHandler("resolved", resolved_command))
    application.add_handler(CommandHandler("vetemail", vetemail_command))
    return application


async def start_polling() -> None:
    global _application
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return
    _application = build_application()
    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling()


async def stop_polling() -> None:
    global _application
    if _application is None:
        return
    await _application.updater.stop()
    await _application.stop()
    await _application.shutdown()
    _application = None


def send_message_sync(text: str) -> None:
    """Outbound push from synchronous callers (the APScheduler pipeline job
    runs on its own thread, not the FastAPI event loop) — spins up a throwaway
    event loop for the one call."""
    chat_id = get_registered_chat_id()
    if chat_id is None:
        logger.warning("Telegram notification skipped — no registered chat ID (send /start to the bot).")
        return
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram notification skipped — TELEGRAM_BOT_TOKEN not set.")
        return

    async def _send() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=text)

    asyncio.run(_send())
