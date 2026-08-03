"""The app's command surface, with no transport in it.

`telegram_bot` used to hold both the parsing and the claims logic behind every
slash command. It is deleted at the gateway cutover, and the logic is not. So
the pure handlers moved here unchanged and `telegram_bot` imports them; the
gateway reaches the same functions through `dispatch`, called by the plugin via
`/internal/command/<name>`.

**Both transports run the same code.** That is the property worth having during
the week the updater flag stays on (task 4.1) — a behaviour that differs by
transport is a behaviour that will differ after the flag is removed, and nobody
would find out until then.

## The command a button may emit is a closed set

`button_commands.BUTTON_COMMANDS` is that set, and the reason it is closed is
that an unregistered command is **not** an error: the tap reaches the agent as a
chat turn and spends tokens (measured live 2026-08-01, three `/ping` taps, three
model replies). So a card may only build a button whose verb is in that tuple,
`gateway_client.build_buttons` refuses anything else, and the preflight checks
the plugin registered them all.

## `/mark <id> sent` versus `/mark <id> <condition>`

Slice 1's design writes the mark-sent tap as `/mark 7 sent`, while this app's
`/mark` sets the condition text and `/sent` marks sent. Both are true, and
`handle_mark` already reserved one word (`reviewed`) for this reason. `sent` is
now reserved too. The alternative — a `sent` command of its own — spends one of
Telegram's per-chat menu slots on something no human types.

The hazard this creates is small and named: a condition genuinely called "sent"
or "reviewed" cannot be set by button. It can still be set by replying, and
`RESERVED_MARK_WORDS` is one place to look when a condition mysteriously does
not stick.
"""

import html
import logging

from . import claim_card, claim_forms, claim_status, config, db, invoice_matching
from .button_commands import BUTTON_COMMANDS

logger = logging.getLogger(__name__)

# Words `/mark <id> <word>` treats as an instruction rather than a condition.
RESERVED_MARK_WORDS = {"sent", "reviewed"}

# At most this many action cards per /actions run. Unchanged from the PTB path;
# what is never allowed to change is that the remainder is *announced*.
ACTION_CARD_CAP = 10


def is_authorized(username: str | None) -> bool:
    # Telegram usernames are case-insensitive; the API reports display casing
    # (e.g. "Jagberg"), so an exact compare wrongly rejects the real user. This
    # check stays app-side after the cutover (task 4.8) — the gateway having
    # decided to deliver an event is not the same as this app accepting it.
    authorized = bool(username) and username.lower() == config.TELEGRAM_USERNAME.lower()
    if not authorized:
        logger.warning("command rejected — unauthorized username %r", username)
    return authorized


def register_chat(username: str, chat_id: int) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(username) DO UPDATE SET chat_id = excluded.chat_id",
            (username, chat_id),
        )


def handle_start(username: str | None, chat_id: int) -> str:
    if not is_authorized(username):
        return ""
    register_chat(username, chat_id)
    return "Registered — you'll get claim notifications here."


def handle_mark(username: str | None, claim_id: int, rest: str) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    word = rest.strip().lower()
    if word == "reviewed":
        return claim_forms.mark_reviewed(claim_id)
    if word == "sent":
        # The mark-sent button's payload. One tap marks the whole submission
        # sent — claims sharing a draft_id move together.
        return claim_status.mark_sent(claim_id)
    return claim_forms.set_condition_text(claim_id, rest)


def handle_pet(username: str | None, claim_id: int, pet_name: str) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    with db.get_connection() as conn:
        pet = conn.execute("SELECT * FROM pets WHERE name = ? COLLATE NOCASE", (pet_name,)).fetchone()
    if pet is None:
        return {"ok": False, "message": f"No pet named '{pet_name}'."}
    return claim_forms.assign_pet(claim_id, pet["id"])


def handle_process(username: str | None, claim_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return claim_forms.process_and_report(claim_id)


def handle_sent(username: str | None, claim_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return claim_status.mark_sent(claim_id)


def handle_resolved(username: str | None, claim_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    with db.get_connection() as conn:
        claim = conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None:
        return {"ok": False, "message": f"No claim #{claim_id} found."}
    return claim_status.confirm_resolved(claim_id)


def handle_vetemail(username: str | None, merchant: str, email: str) -> dict:
    if not is_authorized(username):
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


def handle_notvet(username: str | None, pattern: str) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    if not pattern.strip():
        return {"ok": False, "message": "Usage: /notvet <merchant text>"}
    added = db.add_non_vet_pattern(pattern)
    normalised = pattern.strip().lower()
    if added:
        return {"ok": True,
                "message": f"Added to non-vet list: '{normalised}'. Matching charges won't become claims."}
    return {"ok": True, "message": f"'{normalised}' is already on the non-vet list."}


def handle_unmatch(username: str | None, claim_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return invoice_matching.unmatch(claim_id)


def handle_invoice_request_sent(username: str | None, claim_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return claim_status.mark_invoice_request_sent(claim_id)


def handle_dismiss(username: str | None, claim_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return claim_status.dismiss_mismatch(claim_id)


def _command_button(label: str, command: str) -> dict:
    verb = command[1:].split(" ", 1)[0]
    # Belt and braces with build_buttons, which refuses the same thing. Here it
    # is a programming error caught at build time; there it is the last line
    # before a tap reaches a model.
    assert verb in BUTTON_COMMANDS, f"{command!r} is not a registered command"
    return {"label": label, "command": command}


def handle_merge(username: str | None, proposal_id: int) -> dict:
    """One invoice paid over several charges. The argument is a SPLIT PROPOSAL
    id, not a claim id — the one command here that is not claim-keyed, which is
    why it gets its own verb rather than another reserved word on /mark."""
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return invoice_matching.merge_split_proposal(proposal_id)


def handle_reject_merge(username: str | None, proposal_id: int) -> dict:
    if not is_authorized(username):
        return {"ok": False, "message": "Not authorized."}
    return invoice_matching.reject_split_proposal(proposal_id)


def merge_buttons(proposal_id: int) -> list[dict]:
    """Confirm/reject for a one-invoice-several-charges merge. No per-claim
    pick: which claim carries the invoice is bookkeeping — Petcover sees the
    invoice, never the bank charges — and the larger charge carries it."""
    return [_command_button("✅ Merge — one invoice, one claim", f"/merge {proposal_id}"),
            _command_button("❌ Not the same invoice", f"/reject {proposal_id}")]




# --- text builders, moved verbatim from `telegram_bot` 2026-08-02 -------------
# No transport in any of them, and the module holding them is deleted at the
# cutover. `telegram_bot` imports them back so both paths render identically.


def _esc(s: str) -> str:
    """Kept as the identity now that cards are plain text — see
    `_action_card_text`. Retained rather than deleted because every call site
    marks a spot that must be re-escaped if markup ever comes back."""
    return s


def _esc_html(s: str) -> str:
    """HTML-escape for message bodies. quote=False because none of this goes
    into an attribute — escaping apostrophes would render "hasn&#x27;t"."""
    return html.escape(s or "", quote=False)

_ACTION_EMOJI = {
    "split_proposal": "🔀",
    "unmatch": "❌",
    "confirm_resolved": "✅",
    "mark_sent": "📤",
    "invoice_request_sent": "📧",
    "assign_pet": "🐾",
    "set_condition": "⚠️",
    "dismiss_mismatch": "🔍",
}


def _action_card_text(action: dict) -> str:
    """One action as a short HTML card. Always carries the claim #id — Justin
    acts by id (/mark, /pet), and an alert without one is unusable.

    A submission-level action covers several claims (one Gmail draft, one email,
    one tap), so it names the group and itemises the members: they differ in date,
    amount and condition, and a single summary line would hide what's in the
    email Justin is confirming he sent."""
    members = action.get("members")
    head = f"{_ACTION_EMOJI.get(action['kind'], '•')} {_esc(action['title'].upper())}"
    if members:
        lines = [
            head,
            f"{action['group_id']} · {len(members)} claims · ${abs(action['amount']):,.2f}",
        ]
        lines += [
            f"  • #{m['claim_id']} {m['date']} · {_esc(claim_card._vet_name(m["merchant"]))}"
            f" · ${abs(m['amount']):,.2f}{' · ' + _esc(m['condition_text']) if m['condition_text'] else ''}"
            for m in members
        ]
        lines.append(f"{_esc(action['pet_name'] or '')} · oldest {action['date']} ({action['age_days']}d ago)")
    else:
        lines = [
            head,
            f"Claim #{action['claim_id']} · {_esc(claim_card._vet_name(action["merchant"]))}"
            f" · ${abs(action['amount']):,.2f}",
        ]
        who = " · ".join(filter(None, [action["pet_name"], action["condition_text"]]))
        lines.append(f"{_esc(who) + ' · ' if who else ''}{action['date']} ({action['age_days']}d ago)")
    lines.append(f"Blocks: {_esc(action['blocks'])}")
    if action["kind"] == "assign_pet":
        # The buttons can only say ONE pet. An invoice covering two needs a share
        # each, which is a reply, not a tap — and he has to know that's allowed.
        lines.append("Shared invoice? Reply with the pets and one amount.")
    return "\n".join(lines)


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


# --- cards -------------------------------------------------------------------
#
# A card is `{"png": bytes, "caption": str, "buttons": [...]}` or
# `{"text": str, "buttons": [...]}`. The caller renders and sends; building and
# sending are split so the shape can be asserted without a transport.


def history_card(page: int = 1) -> dict | None:
    """(png, caption, buttons) for one page of claim history, or None when there
    is nothing to show. Re-reads the DB per page so a claim that changed since
    the message was sent renders current."""
    rows = claim_status.history_rows()
    if not rows:
        return None
    per_page = claim_card.ROWS_PER_PAGE
    total_pages = max(1, -(-len(rows) // per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    png = claim_card.render(rows[start:start + per_page], page=page, total_rows=len(rows),
                            agg=claim_card.totals(rows))
    buttons = []
    if page > 1:
        buttons.append(_command_button("◀ Prev", f"/history {page - 1}"))
    if page < total_pages:
        buttons.append(_command_button("Next ▶", f"/history {page + 1}"))
    return {"png": png, "caption": f"Claim history — page {page}/{total_pages}", "buttons": buttons}


def _action_buttons(action: dict) -> list[dict]:
    """Command buttons for one action card. Empty means nothing to tap.

    Every verb here is in BUTTON_COMMANDS and registered by the plugin. That is
    not a style rule: an unregistered one reaches the agent as a chat turn, with
    the tap's own token in the prompt — the commit-token-through-a-model path
    D12 exists to prevent.
    """
    kind, claim_id = action["kind"], action["claim_id"]
    if kind == "mark_sent":
        return [_command_button("✅ Mark sent", f"/mark {claim_id} sent")]
    if kind == "assign_pet":
        return [_command_button(f"🐾 {name}", f"/pet {claim_id} {name}") for name in db.list_pet_names()]
    if kind == "confirm_resolved":
        return [_command_button("✅ Resolved", f"/resolve {claim_id}")]
    if kind == "unmatch":
        return [_command_button("❌ Wrong invoice", f"/unmatch {claim_id}")]
    if kind == "invoice_request_sent":
        return [_command_button("📧 I've sent it", f"/invreq {claim_id}")]
    if kind == "dismiss_mismatch":
        return [_command_button("👍 Reviewed", f"/dismiss {claim_id}")]
    if kind == "set_condition":
        # Prior conditions only. "Other" needs free-text capture, which depends
        # on a plugin conditionally claiming an inbound text message — task 0.10,
        # unverified. Until it is, the card says to reply, which is what the
        # pre-gateway flow did anyway once "Other" was tapped.
        return [_command_button(text[:24], f"/mark {claim_id} {text}")
                for text in prior_conditions(action.get("pet_id"))
                if text.strip().lower() not in RESERVED_MARK_WORDS
                and len(f"/mark {claim_id} {text}".encode("utf-8")) <= 58][:4]
    return []


def actions_cards() -> list[dict]:
    """The /actions run, as cards that can all be sent at once.

    Same derivation as the chat agent's `pending_actions`, deliberately: chat
    and cards answering differently is the failure this shares a source to
    avoid.

    **No separate summary message, and no ordered first send.** Each send spawns
    the gateway CLI and costs ~6s — measured live 2026-08-03, and ~2.5s of that
    is connection setup that cannot be amortised because the CLI is one process
    per message. The old shape was a rendered summary card, then N tap cards,
    then two possible notes: four-plus messages and two ordered rounds, which
    Justin measured at ~6s to the summary and ~6s again for the rest.

    So the counts, the truncation notice and the blocked total ride on the FIRST
    tap card, and every card goes out concurrently. Latency becomes one send's
    worth rather than two rounds'.

    What is deliberately NOT lost: the held-back count. A cap nobody is told
    about is a silent truncation, and that rule outranks the message budget —
    it just travels as a line of text now instead of its own message.
    """
    actions = claim_status.pending_actions()
    if not actions:
        return [{"text": "Nothing waiting on you — every claim is with Petcover or closed.", "buttons": []}]

    tappable = [a for a in actions if a["actionable"]]
    shown = tappable[:ACTION_CARD_CAP]
    blocked = [a for a in actions if not a["actionable"]]

    notes = [f"{len(tappable)} to action, {len(blocked)} blocked"]
    if len(tappable) > len(shown):
        notes.append(f"+{len(tappable) - len(shown)} more — run /actions again once these are cleared.")
    if blocked:
        total = sum(abs(a["amount"]) for a in blocked)
        notes.append(f"🚫 {len(blocked)} claims blocked · ${total:,.2f} — {blocked[0]['flag'] or 'blocked'}. "
                     "No button can fix this; it needs the insurer's claim process on file.")

    cards = [{"text": _action_card_text(a), "buttons": _action_buttons(a)} for a in shown]
    if not cards:
        # Everything is blocked. The notes ARE the answer, and there is nothing
        # to tap — but it still has to be said.
        return [{"text": chr(10).join(notes), "buttons": []}]
    cards[0]["text"] = chr(10).join(notes) + chr(10) * 2 + cards[0]["text"]
    return cards


# --- dispatch ----------------------------------------------------------------


def username_chat(username: str | None):
    """The chat a command's flow state belongs to.

    One user, one chat, so the registered chat id is the key — and it is the
    same key the tap path and the notify path already use. Taking it from the
    command's own context instead would give the gateway path a different key
    from the PTB path for the same conversation.
    """
    return db.registered_chat_id()


def _claim_id_and_rest(args: str) -> tuple[int | None, str]:
    head, _, rest = args.strip().partition(" ")
    try:
        return int(head), rest.strip()
    except ValueError:
        return None, args.strip()


def dispatch(name: str, args: str, username: str | None) -> dict:
    """One command in, `{"text": str, "cards": [...]}` out. Sends nothing.

    Split from the route the same way `internal_api.record_event` is: the suite
    drives this directly, and a command's behaviour is then asserted without a
    gateway, a bot token or a network.
    """
    from . import internal_api

    name = (name or "").lstrip("/").lower()
    args = (args or "").strip()
    if not is_authorized(username):
        # Visible refusal, not silence. A tap that did nothing and said nothing
        # is indistinguishable from one that never arrived.
        return {"text": "Not authorized.", "cards": []}

    if name == "confirm":
        return {"text": internal_api.confirm_proposal(args)["message"], "cards": []}

    if name == "history":
        try:
            page = int(args) if args else 1
        except ValueError:
            page = 1
        card = history_card(page)
        return ({"text": "No vet claims in the last 12 months.", "cards": []} if card is None
                else {"text": "", "cards": [card]})

    if name == "actions":
        return {"text": "", "cards": actions_cards()}

    claim_id, rest = _claim_id_and_rest(args)
    if name == "item":
        from . import pending_flows

        word = args.strip().lower()
        if word == "type":
            return {"text": "", "cards": [pending_flows.await_typed_item(username_chat(username))]}
        if word == "skip":
            return {"text": "", "cards": [pending_flows.record_item(username_chat(username), None)]}
        flow = pending_flows.get(username_chat(username), pending_flows.SPLIT)
        if flow is None:
            return {"text": "No item flow is in progress. Tap the split button again.", "cards": []}
        try:
            index = int(word)
        except ValueError:
            return {"text": "Usage: /item <n> | /item type | /item skip", "cards": []}
        conditions = prior_conditions(flow["state"]["pet_id"])
        if not 0 <= index < len(conditions):
            return {"text": f"No prior condition #{index}.", "cards": []}
        return {"text": "", "cards": [pending_flows.record_item(username_chat(username), conditions[index])]}

    if name in ("merge", "reject"):
        if claim_id is None:
            return {"text": f"/{name} needs a proposal id.", "cards": []}
        handler = handle_merge if name == "merge" else handle_reject_merge
        return {"text": handler(username, claim_id)["message"], "cards": []}

    if name in ("mark", "pet", "resolve", "unmatch", "invreq", "dismiss"):
        if claim_id is None:
            return {"text": f"/{name} needs a claim id. Example: /{name} 7", "cards": []}
        if name == "mark":
            return {"text": handle_mark(username, claim_id, rest)["message"], "cards": []}
        if name == "pet":
            if not rest:
                return {"text": f"/pet needs a pet name. Known pets: {', '.join(db.list_pet_names())}",
                        "cards": []}
            return {"text": handle_pet(username, claim_id, rest)["message"], "cards": []}
        if name == "resolve":
            return {"text": handle_resolved(username, claim_id)["message"], "cards": []}
        if name == "unmatch":
            return {"text": handle_unmatch(username, claim_id)["message"], "cards": []}
        if name == "invreq":
            return {"text": handle_invoice_request_sent(username, claim_id)["message"], "cards": []}
        if name == "dismiss":
            return {"text": handle_dismiss(username, claim_id)["message"], "cards": []}

    # Never a silent no-op: an unknown command here means the plugin registered
    # something this app cannot serve, which is a deploy-time mistake worth
    # seeing rather than a shrug.
    logger.error("plugin dispatched an unknown command: %r", name)
    return {"text": f"/{name} is not a command this app serves.", "cards": []}
