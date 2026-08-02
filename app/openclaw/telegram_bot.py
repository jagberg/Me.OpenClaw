import asyncio
import html
import json
import logging
import re
from datetime import datetime, timezone

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    filters,
)

from . import (agent, claim_card, claim_forms, claim_status, commands, config, db,
               invoice_matching, llm, message_log, proposals)

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "/mark <claim_id> <condition text> — set the condition being claimed for\n"
    "/mark <claim_id> reviewed — confirm a drafted claim looks right (does not send it)\n"
    "/pet <claim_id> <pet name> — assign a pet to a vet-flagged transaction\n"
    "/process <claim_id> — run the matched→drafted advance now\n"
    "/sent <claim_id> — mark a drafted claim as sent (starts Petcover reply tracking)\n"
    "/resolved <claim_id> — confirm you've dealt with an info request/suspension\n"
    "/vetemail <merchant name> <email> — set a vet's contact address for invoice requests\n"
    "/notvet <merchant text> — mark a merchant as not-a-vet so its charges never become claims\n"
    "/history — browse the past year of vet claims, paged\n"
    "/actions — everything waiting on you, with tap-to-resolve cards"
)

_application: Application | None = None
# Holds the startup replay task: create_task alone doesn't keep a strong
# reference, and a GC'd task stops mid-replay with nothing in the log.
_replay_task: "asyncio.Task | None" = None


async def _ack(message) -> None:
    """React 👍 to the user's message so slow handlers (LLM chat) don't feel dead.
    An ack failure must never break the real handler — log and continue."""
    try:
        await message.set_reaction("👍")
    except Exception as exc:
        logger.warning("could not add 👍 reaction ack: %s", exc)


async def _ack_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -1 handler: acks every user-authored message (text, commands,
    uploads) before the real handlers run. Callback taps carry no update.message
    so they're excluded — they already get feedback via _append_result."""
    username = update.effective_user.username if update.effective_user else None
    message = update.effective_message
    if message and _is_authorized(username):
        # Diagnostic trail, not an archive: enough to reconstruct what was asked
        # when a session goes wrong. Nothing persisted this before, so a whole
        # morning's conversation was unrecoverable.
        logger.info("telegram in: %r", (message.text or "<non-text>")[:200])
        await _ack(message)


def get_registered_chat_id() -> int | None:
    return db.registered_chat_id()


# The pure command logic lives in `commands`, which has no transport in it and
# survives this module's deletion at the cutover. Aliased rather than wrapped so
# the PTB handlers below and the gateway's `/internal/command/<name>` dispatcher
# call the identical function — a behaviour that differs by transport is one
# that will differ after the updater flag is removed, and nobody would find out
# until then.
_is_authorized = commands.is_authorized
register_chat = commands.register_chat
handle_start = commands.handle_start
handle_mark = commands.handle_mark
handle_pet = commands.handle_pet
handle_process = commands.handle_process
handle_sent = commands.handle_sent
handle_resolved = commands.handle_resolved
handle_vetemail = commands.handle_vetemail
handle_notvet = commands.handle_notvet
_esc = commands._esc
_ACTION_EMOJI = commands._ACTION_EMOJI
_action_card_text = commands._action_card_text
prior_conditions = commands.prior_conditions
ACTION_CARD_CAP = commands.ACTION_CARD_CAP


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


async def notvet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.effective_user.username if update.effective_user else None
    if not context.args:
        await update.message.reply_text("Usage: /notvet <merchant text>  (e.g. /notvet sp vets love pets)")
        return
    result = handle_notvet(username, " ".join(context.args))
    await update.message.reply_text(result["message"])


def _history_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"hist:{page - 1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"hist:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


def _history_page(page: int) -> tuple[bytes, str, int, int] | None:
    """(png, caption, clamped page, total pages), or None when there's nothing
    to show. Re-reads the DB on every page so a claim that changed since the
    message was sent renders current — cheap, and there's no session to hold."""
    rows = claim_status.history_rows()
    if not rows:
        return None
    per_page = claim_card.ROWS_PER_PAGE
    total_pages = max(1, -(-len(rows) // per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    png = claim_card.render(
        rows[start : start + per_page],
        page=page,
        total_rows=len(rows),
        agg=claim_card.totals(rows),  # whole-year header figures, not just this page
    )
    return png, f"Claim history — page {page}/{total_pages}", page, total_pages


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.effective_user.username if update.effective_user else None
    if not _is_authorized(username):
        return
    result = _history_page(1)
    if result is None:
        await update.message.reply_text("No vet claims in the last 12 months.")
        return
    png, caption, page, total_pages = result
    await update.message.reply_photo(photo=png, caption=caption, reply_markup=_history_keyboard(page, total_pages))


def _action_keyboard(action: dict) -> InlineKeyboardMarkup | None:
    """The PTB rendering of `commands._action_buttons`, not a second source.

    Cards are built in the gateway's shape everywhere now; this converts. Two
    builders is how a tap from an action card came to behave differently from
    the same tap on the alert the pipeline pushes, and it is the shape this
    codebase has been bitten by five times.

    **One deliberate, temporary divergence.** `set_condition` also gets an
    "Other" button here, because typing a new condition is free text and a
    `command` button cannot carry it. The gateway equivalent needs a plugin to
    conditionally claim an inbound text message — task 0.10, answered 2026-08-02
    (`before_dispatch` exists and claims) but not yet built. Until it is, the
    PTB path keeps the button and the gateway path says to reply. Delete this
    branch when 12.2 lands, not before: dropping it early loses the only way to
    enter a condition Justin has never used.
    """
    buttons = commands._action_buttons(action)
    rows = [[InlineKeyboardButton(b["label"], callback_data=f"cmd:{b['command']}")] for b in buttons]
    if action["kind"] == "set_condition":
        rows.append([InlineKeyboardButton("✏️ Other", callback_data=f"condother:{action['claim_id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def actions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Renders `commands.actions_cards()`, which is also what the gateway path
    sends. The cap, the held-back count and the blocked summary are decided
    there, once — a second copy here is how chat and cards came to disagree."""
    username = update.effective_user.username if update.effective_user else None
    if not _is_authorized(username):
        return
    for card in commands.actions_cards():
        markup = _cmd_markup(card.get("buttons"))
        if card.get("png") is not None:
            await update.message.reply_photo(photo=card["png"], caption=card.get("caption", "")[:1024],
                                             reply_markup=markup)
        else:
            await update.message.reply_text(card["text"], reply_markup=markup)


def mark_sent_button(claim_id: int) -> InlineKeyboardMarkup:
    """Inline '✅ Mark sent' button for a drafted-batch notification. One tap
    marks the whole submission sent (any claim id in the batch resolves to the
    shared draft), so Justin never types /sent or juggles per-claim ids."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Mark sent", callback_data=f"sent:{claim_id}")]])


def condition_keyboard(claim_id: int, pet_id: int, multi_item: bool = False) -> InlineKeyboardMarkup:
    """Past conditions as one-tap buttons + an 'Other' that prompts for free
    text. callback_data carries an index into prior_conditions (re-queried on
    tap) to stay under Telegram's 64-byte limit — condition text can be long.
    multi_item adds a 'Split by item' button when the invoice has >1 line."""
    buttons = [
        [InlineKeyboardButton(cond[:60], callback_data=f"cond:{claim_id}:{i}")]
        for i, cond in enumerate(prior_conditions(pet_id)[:6])
    ]
    buttons.append([InlineKeyboardButton("✏️ Other (type it)", callback_data=f"condother:{claim_id}")])
    if multi_item:
        buttons.append([InlineKeyboardButton("🔀 Different conditions per item", callback_data=f"split:{claim_id}")])
    return InlineKeyboardMarkup(buttons)


def _invoice_items(claim_id: int) -> list[dict]:
    """Line items for the split flow — itemised list if the extraction split
    them, else the services string as single-description rows with no amount."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT invoice_data FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    invoice = json.loads(row["invoice_data"]) if row and row["invoice_data"] else {}
    items = invoice.get("items")
    if isinstance(items, list) and items:
        return [{"description": it.get("description", "item"), "amount": it.get("amount")} for it in items]
    services = invoice.get("services")
    if isinstance(services, list):
        services = ", ".join(str(s) for s in services)
    return [{"description": s.strip(), "amount": None} for s in services.split(",")] if services else []


# chat_id -> {claim_id, pet_id, items, idx, assigned, await_type} for the
# per-item condition split. In-memory (same trade-off as _pending_condition).
_pending_split: dict[int, dict] = {}


def _item_keyboard(pet_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(cond[:60], callback_data=f"si:{i}")]
        for i, cond in enumerate(prior_conditions(pet_id)[:6])
    ]
    buttons.append([InlineKeyboardButton("✏️ Type", callback_data="sitype")])
    buttons.append([InlineKeyboardButton("🚫 Not claimable", callback_data="siskip")])
    return InlineKeyboardMarkup(buttons)


async def _prompt_current_item(chat_id: int, bot) -> None:
    state = _pending_split[chat_id]
    item = state["items"][state["idx"]]
    amt = f" (${float(item['amount']):.2f})" if item["amount"] is not None else ""
    await bot.send_message(
        chat_id=chat_id,
        text=f"Item {state['idx'] + 1}/{len(state['items'])}: {item['description']}{amt}\nWhich condition?",
        reply_markup=_item_keyboard(state["pet_id"]),
    )


async def _record_and_advance(chat_id: int, condition: str | None, bot) -> None:
    state = _pending_split[chat_id]
    state["items"][state["idx"]]["condition"] = condition
    state["idx"] += 1
    if state["idx"] < len(state["items"]):
        await _prompt_current_item(chat_id, bot)
    else:
        result = claim_forms.apply_item_conditions(state["claim_id"], state["items"])
        _pending_split.pop(chat_id, None)
        await bot.send_message(chat_id=chat_id, text=result["message"])


def wrong_invoice_button(claim_id: int) -> InlineKeyboardMarkup:
    """'❌ Wrong invoice' for a suspicious match — rejects it and re-searches."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Wrong invoice", callback_data=f"unmatch:{claim_id}")]])


def merge_bill_keyboard(proposal_id: int) -> InlineKeyboardMarkup:
    """Confirm/reject for a one-invoice-several-charges merge. No per-claim
    pick: which claim carries the invoice is bookkeeping (Petcover sees the
    invoice, never the bank charges) — the larger charge carries it."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Merge — one invoice, one claim", callback_data=f"mergebill:{proposal_id}")],
            [InlineKeyboardButton("❌ Not the same invoice", callback_data=f"rejectbill:{proposal_id}")],
        ]
    )


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

# token -> proposed action awaiting the user's Confirm tap (from the chat agent).
# In-memory like _pending_condition: a lost entry (restart) just means re-asking.
_pending_actions: dict[str, dict] = {}


_action_seq = 0


def _register_action(proposal: dict) -> str:
    """Opaque counter token. Was `action:claim_id`, which task proposals have no
    value for — and a claim-shaped key silently collapsed two proposals for the
    same claim into one. Well inside Telegram's 64-byte callback_data limit."""
    global _action_seq
    _action_seq += 1
    token = str(_action_seq)
    _pending_actions[token] = proposal
    return token


def _execute_action(proposal: dict) -> str:
    """The card path's way into the one commit switch.

    The switch itself moved to `proposals.execute` on 2026-08-02, when the chat
    path stopped being able to commit inside the MCP surface (ADR-0027). Both
    origins run the same code — a second copy here is exactly the drift this
    module is being deleted to avoid, and the deletion must not take the
    behaviour with it.
    """
    return proposals.execute(proposal)


def _cmd_markup(buttons):
    """Gateway-shape buttons -> a PTB keyboard whose callbacks re-enter
    `commands.dispatch`. One converter, used by the callback path and by
    `notify`, so the two cannot render the same card differently."""
    from . import notify

    return notify._to_ptb_markup(buttons)


async def _append_result(query, suffix: str) -> None:
    """Append a result line to the tapped message. Keyboard messages may be
    plain text OR a document with caption (merge/review alerts carry the PDF) —
    edit_message_text crashes on the latter, so fall back to the caption."""
    msg = query.message
    if msg.text is not None:
        await query.edit_message_text(text=f"{msg.text}\n\n{suffix}")
    else:
        await query.edit_message_caption(caption=f"{msg.caption or ''}\n\n{suffix}"[:1024])


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    username = query.from_user.username if query.from_user else None
    data = query.data or ""
    # Taps left no trace at all, so "did my button press register?" was only
    # answerable by diffing the DB. Log arrival before anything can reject it.
    logger.info("telegram tap: %r from %r", data[:64], username)
    if not _is_authorized(username):
        logger.warning("telegram tap ignored — %r not authorized", username)
        return
    if data.startswith("cmd:"):
        # Cards are built in the gateway's shape everywhere now — a command
        # string, not a bespoke callback token — so this path runs the same
        # `commands.dispatch` the plugin reaches. A Telegram inline button
        # cannot invoke a slash command, hence the `cmd:` wrapper; after the
        # cutover the wrapper goes and the command travels natively.
        #
        # Doing it this way means the 58-byte budget and the registered-verb
        # rule are exercised on every send from today, a week before they can
        # bite in production.
        command = data.split(":", 1)[1]
        name, _, args = command.lstrip("/").partition(" ")
        outcome = commands.dispatch(name, args, username)
        for card in outcome["cards"]:
            markup = _cmd_markup(card.get("buttons"))
            if card.get("png") is not None:
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=card["png"],
                                             caption=card.get("caption", "")[:1024], reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=query.message.chat_id, text=card["text"],
                                               reply_markup=markup)
        if outcome["text"]:
            await _append_result(query, outcome["text"])
    elif data.startswith("sent:"):
        result = claim_status.mark_sent(int(data.split(":", 1)[1]))
        await _append_result(query, f"✅ {result['message']}")
    elif data.startswith("cond:"):
        _, cid, idx = data.split(":")
        cid, idx = int(cid), int(idx)
        with db.get_connection() as conn:
            claim = conn.execute("SELECT pet_id FROM vet_claims WHERE id = ?", (cid,)).fetchone()
        conds = prior_conditions(claim["pet_id"]) if claim else []
        if 0 <= idx < len(conds):
            result = claim_forms.set_condition_text(cid, conds[idx])
            await _append_result(query, f"✅ {result['message']}")
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
        await _append_result(query, f"✅ {result['message']}")
    elif data.startswith("unmatch:"):
        result = invoice_matching.unmatch(int(data.split(":", 1)[1]))
        await _append_result(query, f"❌ {result['message']}")
    elif data.startswith("usebill:"):
        # legacy pick buttons from already-sent messages — still honored
        _, proposal_id, claim_id = data.split(":")
        result = invoice_matching.resolve_split_proposal(int(proposal_id), int(claim_id))
        icon = "✅" if result["ok"] else "⚠️"
        await _append_result(query, f"{icon} {result['message']}")
    elif data.startswith("mergebill:"):
        result = invoice_matching.merge_split_proposal(int(data.split(":", 1)[1]))
        icon = "✅" if result["ok"] else "⚠️"
        await _append_result(query, f"{icon} {result['message']}")
    elif data.startswith("rejectbill:"):
        result = invoice_matching.reject_split_proposal(int(data.split(":", 1)[1]))
        icon = "❌" if result["ok"] else "⚠️"
        await _append_result(query, f"{icon} {result['message']}")
    elif data.startswith("split:"):
        cid = int(data.split(":", 1)[1])
        with db.get_connection() as conn:
            pet = conn.execute("SELECT pet_id FROM vet_claims WHERE id = ?", (cid,)).fetchone()
        items = _invoice_items(cid)
        if not items or not pet or pet["pet_id"] is None:
            await context.bot.send_message(chat_id=query.message.chat_id, text="Can't split — no line items or pet.")
            return
        _pending_split[query.message.chat_id] = {"claim_id": cid, "pet_id": pet["pet_id"], "items": items, "idx": 0}
        await _prompt_current_item(query.message.chat_id, context.bot)
    elif data == "siskip":
        if query.message.chat_id in _pending_split:
            await _record_and_advance(query.message.chat_id, None, context.bot)
    elif data == "sitype":
        state = _pending_split.get(query.message.chat_id)
        if state:
            state["await_type"] = True
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Reply with the condition for this item:", reply_markup=ForceReply()
            )
    elif data.startswith("si:"):
        state = _pending_split.get(query.message.chat_id)
        if state:
            conds = prior_conditions(state["pet_id"])
            idx = int(data.split(":", 1)[1])
            if 0 <= idx < len(conds):
                await _record_and_advance(query.message.chat_id, conds[idx], context.bot)
    elif data.startswith("act:"):
        proposal = _pending_actions.pop(data.split(":", 1)[1], None)
        if proposal is None:
            await _append_result(query, "⚠️ Action expired — ask again.")
            return
        message = _execute_action(proposal)
        await _append_result(query, f"✅ {message}")
    elif data.startswith("invreq:"):
        result = claim_status.mark_invoice_request_sent(int(data.split(":", 1)[1]))
        await _append_result(query, f"{'📧' if result['ok'] else '⚠️'} {result['message']}")
    elif data.startswith("dismiss:"):
        result = claim_status.dismiss_mismatch(int(data.split(":", 1)[1]))
        await _append_result(query, f"{'👍' if result['ok'] else '⚠️'} {result['message']}")
    elif data.startswith("resolved:"):
        result = claim_status.confirm_resolved(int(data.split(":", 1)[1]))
        await _append_result(query, f"{'✅' if result['ok'] else '⚠️'} {result['message']}")
    elif data.startswith("hist:"):
        result = _history_page(int(data.split(":", 1)[1]))
        if result is None:
            return
        png, caption, page, total_pages = result
        await query.edit_message_media(
            media=InputMediaPhoto(media=png, caption=caption),
            reply_markup=_history_keyboard(page, total_pages),
        )
    else:
        # A button whose prefix nobody handles used to do nothing, silently —
        # indistinguishable from a tap that never arrived.
        logger.error("telegram tap: unhandled callback_data %r", data[:64])
        await _append_result(query, "⚠️ That button isn't wired up — tell Claude.")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Without this PTB swallows handler exceptions into its own logger with no
    user feedback: the tap looked accepted and nothing happened."""
    logger.error("telegram handler failed on %r", update, exc_info=context.error)
    # PTB swallows handler exceptions into process_error, so process_update
    # returns cleanly and would mark this update done. Record the failure here
    # and leave processed_at NULL so the update is retried at next startup.
    message_log.mark_failed(getattr(update, "update_id", None), context.error)
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await _append_result(query, f"⚠️ Failed: {context.error}")
        except Exception:  # noqa: BLE001 — feedback is best-effort; the log above is the record
            logger.exception("could not report failure back to Telegram")


_CLAIM_IN_CARD = re.compile(r"claim #(\d+)", re.IGNORECASE)
# Callback prefixes whose first field is a claim id. `act:` (proposal token),
# `hist:`, `si*` and the *bill: (proposal id) tokens are NOT claims — reading a
# claim id out of them would target whatever row shares that number.
_CLAIM_CALLBACK_PREFIXES = frozenset(
    {"sent", "cond", "condother", "setpet", "unmatch", "split", "invreq", "dismiss", "resolved"}
)


def _replied_to_claim_id(message) -> int | None:
    """The claim of the card this message replies to, or None.

    Cards name it in their text — or their caption, since PDF alerts have no
    text — and again in their buttons' callback data. None unless EXACTLY one
    claim is named: a submission-level card names every member, and picking one
    of those would act on a claim Justin wasn't looking at.

    Without this, a reply to the ASSIGN PET card was a message with no target:
    the agent asked for "the reference" three times and then fabricated
    arguments out of its own tool schema (2026-07-27)."""
    parent = getattr(message, "reply_to_message", None)
    if parent is None:
        return None
    body = f"{getattr(parent, 'text', None) or ''}\n{getattr(parent, 'caption', None) or ''}"
    ids = {int(found) for found in _CLAIM_IN_CARD.findall(body)}
    markup = getattr(parent, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or ():
        for button in row:
            fields = (getattr(button, "callback_data", None) or "").split(":")
            if len(fields) >= 2 and fields[0] in _CLAIM_CALLBACK_PREFIXES and fields[1].isdigit():
                ids.add(int(fields[1]))
    return ids.pop() if len(ids) == 1 else None


async def on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text condition entry: after 'Other', the next plain-text message
    from the authorized user sets the condition for the pending claim."""
    username = update.effective_user.username if update.effective_user else None
    if not _is_authorized(username):
        return
    chat_id = update.effective_chat.id
    # effective_message, not .message: an edit is an `edited_message` update
    # where .message is None, which crashed this handler with
    # "'NoneType' object has no attribute 'text'" (2026-07-27) and lost the
    # correction Justin had just made.
    message = update.effective_message
    text = message.text.strip()
    # A typed condition for the current item in a split flow takes priority.
    split = _pending_split.get(chat_id)
    if split and split.get("await_type"):
        split["await_type"] = False
        await _record_and_advance(chat_id, text, context.bot)
        return
    claim_id = _pending_condition.pop(chat_id, None)
    if claim_id is not None:
        result = claim_forms.set_condition_text(claim_id, text)
        await message.reply_text(result["message"])
        return
    # No pending typed-reply flow owns this message — treat it as free-form chat.
    await _handle_chat(update)


async def _handle_chat(update: Update) -> None:
    """Free-form message → conversational agent. A proposed mutation comes back
    as a Confirm button; the write happens only on the tap (see on_callback)."""
    message = update.effective_message
    try:
        reply, proposal = await asyncio.to_thread(
            agent.handle_message, message.text, update.effective_chat.id,
            _replied_to_claim_id(message),
        )
    except llm.LLMUnavailableError as exc:
        await message.reply_text(f"⚠️ LLM unavailable: {exc}")
        return
    except Exception as exc:  # never leave the user staring at silence
        logger.exception("chat handler failed")
        await message.reply_text(f"⚠️ Something broke handling that: {type(exc).__name__}: {exc}")
        return
    if proposal:
        token = _register_action(proposal)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data=f"act:{token}")]])
        await message.reply_text(reply or f"Confirm: {proposal['label']}?", reply_markup=markup)
    else:
        await message.reply_text(reply or "…")


class LoggedApplication(Application):
    """Records every inbound update before handlers touch it.

    One seam covers commands, taps, free text and uploads. The row is written
    *first* so a crash mid-handler leaves processed_at NULL and the update gets
    replayed at startup — that's the whole durability story.

    PTB routes handler exceptions to process_error, so they never surface here;
    _on_error is what stamps the failure on the row.
    """

    async def process_update(self, update: object) -> None:
        update_id = message_log.record_inbound(update)
        await super().process_update(update)
        message_log.mark_processed(update_id)


class LoggedBot(ExtBot):
    """Records every outbound message. Overrides the public senders only —
    reply_text and friends funnel through these, so handlers stay untouched and
    no private PTB internals can break on upgrade. Photo/document bytes are
    summarised by size, never inlined into the log."""

    async def send_message(self, chat_id, text, *args, **kwargs):
        message_log.record_outbound("send_message", text, {"chat_id": chat_id, "text": text})
        return await super().send_message(chat_id, text, *args, **kwargs)

    async def send_photo(self, chat_id, photo, *args, caption=None, **kwargs):
        message_log.record_outbound(
            "send_photo", caption or "", {"chat_id": chat_id, "caption": caption, "photo": _blob_size(photo)}
        )
        return await super().send_photo(chat_id, photo, *args, caption=caption, **kwargs)

    async def send_document(self, chat_id, document, *args, caption=None, **kwargs):
        message_log.record_outbound(
            "send_document", caption or "", {"chat_id": chat_id, "caption": caption, "document": _blob_size(document)}
        )
        return await super().send_document(chat_id, document, *args, caption=caption, **kwargs)

    async def edit_message_text(self, text, *args, **kwargs):
        message_log.record_outbound("edit_message_text", text, {"text": text})
        return await super().edit_message_text(text, *args, **kwargs)

    async def edit_message_caption(self, *args, caption=None, **kwargs):
        message_log.record_outbound("edit_message_caption", caption or "", {"caption": caption})
        return await super().edit_message_caption(*args, caption=caption, **kwargs)


def _blob_size(blob) -> str:
    try:
        return f"<{len(blob)} bytes>"
    except TypeError:
        return f"<{type(blob).__name__}>"


def build_application() -> Application:
    application = (
        Application.builder()
        .application_class(LoggedApplication)
        .bot(LoggedBot(config.TELEGRAM_BOT_TOKEN))
        .build()
    )
    # Group -1 runs before all group-0 handlers: instant 👍 receipt ack.
    application.add_handler(MessageHandler(filters.ALL, _ack_user_message), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mark", mark_command))
    application.add_handler(CommandHandler("pet", pet_command))
    application.add_handler(CommandHandler("process", process_command))
    application.add_handler(CommandHandler("sent", sent_command))
    application.add_handler(CommandHandler("resolved", resolved_command))
    application.add_handler(CommandHandler("vetemail", vetemail_command))
    application.add_handler(CommandHandler("notvet", notvet_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("actions", actions_command))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_reply))
    application.add_error_handler(_on_error)
    return application


def _log_replay_outcome(task: asyncio.Task) -> None:
    """ERROR, not a warning: a failed replay means logged messages Justin sent
    were never acted on, and only he can re-send them."""
    if task.cancelled():
        logger.warning("Telegram replay was cancelled — queued updates stay queued")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("replaying queued Telegram updates failed: %s", exc, exc_info=exc)


async def start_polling() -> None:
    global _application
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return
    _application = build_application()
    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling()
    # Replay after polling starts: anything Telegram redelivers itself is then
    # deduped by update_id instead of being logged and acted on twice.
    #
    # NOT awaited: replaying a queued chat message runs a full LLM turn, and this
    # runs inside the FastAPI lifespan — so awaiting it held HTTP startup for
    # ~30s on 2026-07-27 (uvicorn hadn't bound, /health refused the connection,
    # and deploy.ps1 read that as a failed deploy). A slow or retrying provider
    # would hold it for minutes. Kept in a module reference so the task isn't
    # garbage-collected mid-flight, with its outcome logged — a fire-and-forget
    # task's death is otherwise silent (same trap as the updater).
    global _replay_task
    _replay_task = asyncio.create_task(message_log.replay_pending(_application))
    _replay_task.add_done_callback(_log_replay_outcome)


def polling_alive() -> bool | None:
    """True/False once the bot is configured, None when it's disabled. The
    updater task is fire-and-forget: if it dies (host suspend killed the long
    poll), inbound taps stop arriving with nothing in the logs. Probing
    getUpdates from outside can't tell — it races the gap between polls."""
    if _application is None:
        return None
    return bool(_application.updater.running)


async def stop_polling() -> None:
    global _application
    if _application is None:
        return
    await _application.updater.stop()
    await _application.stop()
    await _application.shutdown()
    _application = None


def send_document_sync(caption: str, document: bytes, filename: str, reply_markup=None) -> None:
    """Outbound document push (e.g. the PDF invoice a review alert is about),
    same synchronous-caller pattern as send_message_sync. Telegram caps
    captions at 1024 chars — truncated, the document is the point."""
    chat_id = get_registered_chat_id()
    if chat_id is None or not config.TELEGRAM_BOT_TOKEN:
        logger.error("Telegram document skipped — no chat id or token; output is being dropped.")
        return

    async def _send() -> None:
        bot = LoggedBot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_document(
            chat_id=chat_id, document=document, filename=filename,
            caption=caption[:1024], reply_markup=reply_markup,
        )

    asyncio.run(_send())


def send_photo_sync(caption: str, photo: bytes, reply_markup=None) -> None:
    """Push a rendered card image from a synchronous caller (the scheduler's
    nudge job). Same throwaway-loop pattern as send_message_sync."""
    chat_id = get_registered_chat_id()
    if chat_id is None or not config.TELEGRAM_BOT_TOKEN:
        logger.error("Telegram photo skipped — no chat id or token; output is being dropped.")
        return

    async def _send() -> None:
        bot = LoggedBot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption[:1024], reply_markup=reply_markup)

    asyncio.run(_send())


def send_message_sync(text: str, reply_markup=None) -> None:
    """Outbound push from synchronous callers (the APScheduler pipeline job
    runs on its own thread, not the FastAPI event loop) — spins up a throwaway
    event loop for the one call. Optional reply_markup attaches inline buttons."""
    chat_id = get_registered_chat_id()
    if chat_id is None:
        logger.error("Telegram notification skipped — no registered chat ID; send /start to the bot.")
        return
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("Telegram notification skipped — TELEGRAM_BOT_TOKEN not set.")
        return

    async def _send() -> None:
        bot = LoggedBot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    asyncio.run(_send())
