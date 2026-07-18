import asyncio
import logging
from datetime import datetime, timezone

from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import claim_forms, claim_status, config, db, invoice_matching

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
    # Telegram usernames are case-insensitive; the API reports display casing
    # (e.g. "Jagberg"), so an exact compare wrongly rejects the real user.
    authorized = bool(username) and username.lower() == config.TELEGRAM_USERNAME.lower()
    if not authorized:
        logger.warning("Telegram update rejected — unauthorized username %r", username)
    return authorized


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


def mark_sent_button(claim_id: int) -> InlineKeyboardMarkup:
    """Inline '✅ Mark sent' button for a drafted-batch notification. One tap
    marks the whole submission sent (any claim id in the batch resolves to the
    shared draft), so Justin never types /sent or juggles per-claim ids."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Mark sent", callback_data=f"sent:{claim_id}")]])


def prior_conditions(pet_id: int) -> list[str]:
    """Conditions Justin has claimed for this pet before — offered as tap
    options so a repeat condition (arthritis, ear infection…) is one tap, not
    retyping. This is the reusable condition history the original spec deferred."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT condition_text FROM vet_claims "
            "WHERE pet_id = ? AND condition_text IS NOT NULL AND condition_text != '' "
            "ORDER BY condition_text",
            (pet_id,),
        ).fetchall()
    return [r["condition_text"] for r in rows]


def condition_keyboard(claim_id: int, pet_id: int) -> InlineKeyboardMarkup:
    """Past conditions as one-tap buttons + an 'Other' that prompts for free
    text. callback_data carries an index into prior_conditions (re-queried on
    tap) to stay under Telegram's 64-byte limit — condition text can be long."""
    buttons = [
        [InlineKeyboardButton(cond[:60], callback_data=f"cond:{claim_id}:{i}")]
        for i, cond in enumerate(prior_conditions(pet_id)[:6])
    ]
    buttons.append([InlineKeyboardButton("✏️ Other (type it)", callback_data=f"condother:{claim_id}")])
    return InlineKeyboardMarkup(buttons)


def wrong_invoice_button(claim_id: int) -> InlineKeyboardMarkup:
    """'❌ Wrong invoice' for a suspicious match — rejects it and re-searches."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Wrong invoice", callback_data=f"unmatch:{claim_id}")]])


def pet_keyboard(claim_id: int) -> InlineKeyboardMarkup:
    """One button per known pet, to assign an unattributed claim in a tap."""
    with db.get_connection() as conn:
        pets = conn.execute("SELECT id, name FROM pets ORDER BY name").fetchall()
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(p["name"], callback_data=f"setpet:{claim_id}:{p['id']}")] for p in pets]
    )


# chat_id -> claim_id awaiting a free-text condition reply. In-memory: a lost
# entry (container restart) just means Justin taps the button again.
_pending_condition: dict[int, int] = {}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    username = query.from_user.username if query.from_user else None
    if not _is_authorized(username):
        return
    data = query.data or ""
    if data.startswith("sent:"):
        result = claim_status.mark_sent(int(data.split(":", 1)[1]))
        await query.edit_message_text(text=f"{query.message.text}\n\n✅ {result['message']}")
    elif data.startswith("cond:"):
        _, cid, idx = data.split(":")
        cid, idx = int(cid), int(idx)
        with db.get_connection() as conn:
            claim = conn.execute("SELECT pet_id FROM vet_claims WHERE id = ?", (cid,)).fetchone()
        conds = prior_conditions(claim["pet_id"]) if claim else []
        if 0 <= idx < len(conds):
            result = claim_forms.set_condition_text(cid, conds[idx])
            await query.edit_message_text(text=f"{query.message.text}\n\n✅ {result['message']}")
    elif data.startswith("condother:"):
        _pending_condition[query.message.chat_id] = int(data.split(":", 1)[1])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Reply to this message with the condition being claimed:",
            reply_markup=ForceReply(),
        )
    elif data.startswith("setpet:"):
        _, cid, pet_id = data.split(":")
        result = claim_forms.assign_pet(int(cid), int(pet_id))
        await query.edit_message_text(text=f"{query.message.text}\n\n✅ {result['message']}")
    elif data.startswith("unmatch:"):
        result = invoice_matching.unmatch(int(data.split(":", 1)[1]))
        await query.edit_message_text(text=f"{query.message.text}\n\n❌ {result['message']}")


async def on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text condition entry: after 'Other', the next plain-text message
    from the authorized user sets the condition for the pending claim."""
    username = update.effective_user.username if update.effective_user else None
    if not _is_authorized(username):
        return
    claim_id = _pending_condition.pop(update.effective_chat.id, None)
    if claim_id is None:
        return
    result = claim_forms.set_condition_text(claim_id, update.message.text.strip())
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
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_reply))
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


def send_message_sync(text: str, reply_markup=None) -> None:
    """Outbound push from synchronous callers (the APScheduler pipeline job
    runs on its own thread, not the FastAPI event loop) — spins up a throwaway
    event loop for the one call. Optional reply_markup attaches inline buttons."""
    chat_id = get_registered_chat_id()
    if chat_id is None:
        logger.warning("Telegram notification skipped — no registered chat ID (send /start to the bot).")
        return
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram notification skipped — TELEGRAM_BOT_TOKEN not set.")
        return

    async def _send() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    asyncio.run(_send())
