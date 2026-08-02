"""Flows that own Justin's next typed message, and the decision to claim it.

Two exist: entering a condition free-hand, and walking a multi-item invoice one
line at a time. Both work the same way — a tap starts the flow, and the *next*
thing he types belongs to it rather than to the chat agent.

## Why this is durable now

It was two module-level dicts in `telegram_bot`, keyed by chat id, and the
comment said a lost entry just means tapping the button again. That was true
while one process owned the tap, the state and the reply.

After the cutover it is not. The tap arrives at the gateway, the app is asked
whether to claim the message over HTTP, and the reply comes back through a
third hop — so the state has to outlive a request, and a restart in between
must not silently turn a typed condition into a chat turn. That is worse than
losing the flow: `condition_text` is a field the hard rules forbid inferring,
and the chat agent would cheerfully interpret the words.

## Why the app decides, not the plugin

`claim_text` is the whole decision and it lives here, in Python, next to the
data. The plugin asks and obeys. A plugin that decided for itself would be a
second copy of "is a flow pending", and this codebase has been bitten five
times by second copies.

The gateway hook that makes this possible is `before_dispatch` — *"inspect or
handle a message before model dispatch; first handler returning
`{ handled: true }` wins"* — reached from a plugin via `api.registerHook`. It
runs **after** command routing, so a slash command still works while a flow is
pending, which is the reason it beats `inbound_claim` here.
"""

import json
import logging
from datetime import datetime, timezone

from . import claim_forms, db

logger = logging.getLogger(__name__)

CONDITION = "condition"
SPLIT = "split"
KINDS = (CONDITION, SPLIT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _put(chat_id, kind: str, claim_id, state: dict) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown flow kind: {kind!r}")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO pending_flows (chat_id, kind, claim_id, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, kind) DO UPDATE SET claim_id = excluded.claim_id, "
            "state = excluded.state, updated_at = excluded.updated_at",
            (str(chat_id), kind, claim_id, json.dumps(state), _now(), _now()),
        )


def get(chat_id, kind: str) -> dict | None:
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM pending_flows WHERE chat_id = ? AND kind = ?",
                           (str(chat_id), kind)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["state"] = json.loads(out["state"])
    return out


def clear(chat_id, kind: str) -> None:
    with db.get_connection() as conn:
        conn.execute("DELETE FROM pending_flows WHERE chat_id = ? AND kind = ?", (str(chat_id), kind))


def start_condition(chat_id, claim_id: int) -> dict:
    """Justin tapped 'Other' — the next thing he types is the condition."""
    _put(chat_id, CONDITION, int(claim_id), {})
    return {"prompt": "Reply to this message with the condition being claimed:", "force_reply": True}


def start_split(chat_id, claim_id: int, pet_id: int, items: list[dict]) -> dict:
    _put(chat_id, SPLIT, int(claim_id), {"pet_id": pet_id, "items": items, "idx": 0})
    return item_prompt(chat_id)


def item_prompt(chat_id) -> dict | None:
    """The current item's question, as a card the caller renders."""
    flow = get(chat_id, SPLIT)
    if flow is None:
        return None
    state = flow["state"]
    item = state["items"][state["idx"]]
    amount = f" (${float(item['amount']):.2f})" if item.get("amount") is not None else ""
    return {
        "prompt": f"Item {state['idx'] + 1}/{len(state['items'])}: {item['description']}{amount}\n"
                  "Which condition?",
        "buttons": _item_buttons(state["pet_id"]),
    }


def _item_buttons(pet_id: int) -> list[dict]:
    from . import commands

    buttons = [{"label": text[:60], "command": f"/item {i}"}
               for i, text in enumerate(commands.prior_conditions(pet_id)[:6])
               if len(f"/item {i}".encode("utf-8")) <= 58]
    buttons.append({"label": "✏️ Type", "command": "/item type"})
    buttons.append({"label": "🚫 Not claimable", "command": "/item skip"})
    return buttons


def record_item(chat_id, condition: str | None) -> dict:
    """Answer the current item and advance. Returns the next prompt, or the
    finished result when the last item is answered."""
    flow = get(chat_id, SPLIT)
    if flow is None:
        return {"text": "No item flow is in progress. Tap the split button again."}
    state = flow["state"]
    state["items"][state["idx"]]["condition"] = condition
    state["idx"] += 1
    if state["idx"] < len(state["items"]):
        _put(chat_id, SPLIT, flow["claim_id"], state)
        return item_prompt(chat_id) or {"text": "Item flow lost — tap the split button again."}
    # Last item: apply, then clear. Applied BEFORE the clear so a failure leaves
    # the flow in place to retry rather than dropping the answers on the floor.
    result = claim_forms.apply_item_conditions(flow["claim_id"], state["items"])
    clear(chat_id, SPLIT)
    return {"text": result["message"]}


def awaiting_typed_item(chat_id) -> bool:
    flow = get(chat_id, SPLIT)
    return bool(flow and flow["state"].get("await_type"))


def await_typed_item(chat_id) -> dict:
    flow = get(chat_id, SPLIT)
    if flow is None:
        return {"text": "No item flow is in progress."}
    state = dict(flow["state"], await_type=True)
    _put(chat_id, SPLIT, flow["claim_id"], state)
    return {"prompt": "Reply with the condition for this item:", "force_reply": True}


def claim_text(chat_id, text: str) -> dict | None:
    """**The** decision: does a pending flow own this message?

    `None` means no — the caller passes it on to the agent. Anything else means
    the flow consumed it and the returned card is the answer.

    A typed item in a split flow wins over a pending condition: the split is the
    more specific state and was entered more recently by construction.
    """
    text = (text or "").strip()
    if not text:
        return None

    if awaiting_typed_item(chat_id):
        flow = get(chat_id, SPLIT)
        state = dict(flow["state"], await_type=False)
        _put(chat_id, SPLIT, flow["claim_id"], state)
        logger.info("pending flow: split item consumed a typed reply for claim #%s", flow["claim_id"])
        return record_item(chat_id, text)

    flow = get(chat_id, CONDITION)
    if flow is not None:
        clear(chat_id, CONDITION)
        # Stored verbatim. No model between his words and `condition_text` —
        # the field the hard rules forbid inferring.
        result = claim_forms.set_condition_text(flow["claim_id"], text)
        logger.info("pending flow: condition consumed a typed reply for claim #%s", flow["claim_id"])
        return {"text": result["message"]}

    return None
