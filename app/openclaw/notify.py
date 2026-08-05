"""The one outbound seam, and the switch between the two transports.

Everything the app pushes to Justin unprompted — claim notifications, the daily
nudge, review alerts with a PDF, action cards — goes through here. Before this,
callers reached `telegram_bot.send_*_sync` directly, which is fine while there
is one transport and impossible once there are two.

**Why a switch and not a cutover.** `TELEGRAM_UPDATER_ENABLED` stays on for one
week of real daily use after the cutover (task 4.1, Justin's call 2026-08-01),
because the failures only real use finds are the ones that matter: a caption
that will not edit, buttons that will not attach to a card, a tap that quietly
reached the LLM. Rollback is one env var and a restart. Section 6 deletes the
flag and the PTB half with it.

**Buttons differ by transport, and that is the whole risk.** PTB takes an
`InlineKeyboardMarkup` built from callback data; the gateway takes command
buttons whose verbs must be registered. Callers therefore hand this module the
*gateway* shape — `[{"label": ..., "command": "/mark 7 sent"}]` — and the PTB
path converts. That direction is deliberate: the gateway shape is the one with
a hard constraint (58 UTF-8 bytes, registered verbs only), so building it
everywhere means the constraint is exercised on every send from today, a week
before it can bite in production.

**A dropped message is always visible.** Every path here logs at ERROR and
returns False rather than raising: an unattended pipeline tick must not die
because a notification failed, and it must never look like it sent one.
"""

import logging

from . import config, db, trace

logger = logging.getLogger(__name__)


def using_gateway() -> bool:
    """True when the gateway owns the channel. The flag is the app updater's,
    so the transports are exact opposites — two pollers on one token is a 409
    and `scripts/gateway_preflight.py` fails the deploy for it."""
    return not config.TELEGRAM_UPDATER_ENABLED


def _target() -> str | None:
    chat_id = db.registered_chat_id()
    if chat_id is None:
        # Not a warning. Nothing reaches Justin at all until this is fixed, and
        # the fix is a human action (`/start` to the bot).
        logger.error("outbound dropped — no registered chat ID; send /start to the bot")
        return None
    return str(chat_id)


def _to_ptb_markup(buttons: list[dict] | None):
    """Gateway buttons -> an InlineKeyboardMarkup carrying the same commands.

    A Telegram inline button cannot invoke a slash command, so the callback data
    is `cmd:<command>` and `telegram_bot.on_callback` runs it through the same
    `commands.dispatch` the gateway path uses. One row per button: labels here
    are short and the count is small.
    """
    if not buttons:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b["label"], callback_data=f"cmd:{b['command']}")] for b in buttons]
    )


def send_text(text: str, buttons: list[dict] | None = None) -> bool:
    target = _target()
    if target is None:
        return False
    try:
        with trace.step("notify.text", transport="gateway" if using_gateway() else "ptb"):
            if using_gateway():
                from . import gateway_client

                if gateway_client.using_http_route():
                    gateway_client.send_cards(target, [{"text": text, "buttons": buttons}])
                else:
                    gateway_client.send_message(target, text, buttons=buttons)
            else:
                from . import telegram_bot

                telegram_bot.send_message_sync(text, reply_markup=_to_ptb_markup(buttons))
    except Exception as exc:  # noqa: BLE001 — a tick must not die on a lost notification
        logger.error("outbound text dropped: %s", exc, exc_info=True)
        return False
    return True


def send_card(caption: str, image: bytes, buttons: list[dict] | None = None) -> bool:
    """A rendered Pillow card. The gateway path publishes to the shared outbox
    and sends a path; the PTB path sends the bytes."""
    target = _target()
    if target is None:
        return False
    try:
        with trace.step("notify.card", transport="gateway" if using_gateway() else "ptb"):
            if using_gateway():
                from . import gateway_client

                if gateway_client.using_http_route():
                    # Same fast path the command route takes. One card, so the
                    # win here is ~9s -> ~1s rather than N rounds -> one.
                    gateway_client.send_cards(
                        target, [{"png": image, "caption": caption, "buttons": buttons}]
                    )
                else:
                    gateway_client.send_card(target, image, caption=caption, buttons=buttons)
            else:
                from . import telegram_bot

                telegram_bot.send_photo_sync(caption, image, reply_markup=_to_ptb_markup(buttons))
    except Exception as exc:  # noqa: BLE001
        logger.error("outbound card dropped: %s", exc, exc_info=True)
        return False
    return True


def send_document(
    caption: str, document: bytes, filename: str, buttons: list[dict] | None = None
) -> bool:
    """The PDF a review alert is about. Telegram caps captions at 1024 — the
    document is the point, so the caption is what gets truncated."""
    target = _target()
    if target is None:
        return False
    try:
        with trace.step("notify.document", transport="gateway" if using_gateway() else "ptb"):
            if using_gateway():
                from . import gateway_client, media_outbox

                path = media_outbox.publish(
                    document, suffix=".pdf", stem=filename.rsplit(".", 1)[0]
                )
                gateway_client.send_file(target, path, caption=caption[:1024], buttons=buttons)
            else:
                from . import telegram_bot

                telegram_bot.send_document_sync(
                    caption, document, filename, reply_markup=_to_ptb_markup(buttons)
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("outbound document dropped: %s", exc, exc_info=True)
        return False
    return True


def ack(message_id, chat_id=None) -> bool:
    """The 👍 on Justin's message, so a slow handler does not feel dead.

    Task 4.7. Returns whether it landed and never raises — losing the ack is
    strictly better than losing the handler, which is why every failure here is
    a WARNING rather than an ERROR.

    **Typed messages only, and that is a platform limit.** A plugin command
    handler's context carries no message id — not under a different name, it is
    simply not in the object the gateway builds (`commands-CDhgE9eG.js`) — and
    commands are routed before any message hook runs. So a tapped button gets no
    reaction; its feedback is the reply text, as before.
    """
    if message_id in (None, ""):
        return False
    target = _target() if chat_id is None else str(chat_id)
    if target is None:
        return False
    try:
        if using_gateway():
            from . import gateway_client

            return gateway_client.react(target, str(message_id))
        from . import telegram_bot

        return telegram_bot.react_sync(target, str(message_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ack failed for message %s: %s", message_id, exc)
        return False


def append_result(message_id, suffix: str, chat_id=None) -> bool:
    """Append a tap's result to the message it was tapped on. Task 4.6.

    **Text only, and that is a platform limit.** The CLI exposes no caption
    flag, so an edit runs `editMode: "auto"` — `editMessageText` first, falling
    back to `editMessageCaption` when Telegram rejects it. Since the cards are
    Pillow images, the caption path is the normal one, and it writes
    `editMessage failed: there is no text in the message to edit` into the
    *gateway's* log on every success. A caption edit that genuinely failed looks
    identical there. Accepted gap, recorded in `docs/gateway-deploy.md`.

    **Falls back to a plain reply.** If the edit fails outright the result still
    has to reach Justin — a tap whose outcome vanished is indistinguishable from
    one that never registered, which is the failure ADR-0014 exists for. The
    degradation is logged so a permanent fallback is visible rather than quietly
    becoming the norm.
    """
    target = _target() if chat_id is None else str(chat_id)
    if target is None:
        return False
    if message_id in (None, ""):
        return send_text(suffix)
    try:
        if using_gateway():
            from . import gateway_client

            gateway_client.edit_message(target, str(message_id), suffix)
        else:
            from . import telegram_bot

            telegram_bot.edit_message_sync(target, str(message_id), suffix)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("edit failed for message %s, falling back to a reply: %s", message_id, exc)
        return send_text(suffix)
