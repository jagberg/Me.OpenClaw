"""Conversational agent for the Telegram bot: read/act tools over the claims
domain, driven by llm.chat's bounded tool loop.

Read tools run immediately and return compact summaries (never raw email dumps —
keeps turns under the provider's 8k context cap). Act tools NEVER mutate: they
record a *proposed action* that the Telegram layer renders as a Confirm button;
the write happens only on the tap (telegram_bot._execute_action). That harness
gate — not the model's good behaviour — is what enforces the hard rules.
"""
import json

from . import db, llm

SYSTEM_PROMPT = (
    "You are OpenClaw's assistant for Justin, over Telegram. You help interrogate and act on "
    "pet-insurance claims and their Petcover email replies.\n"
    "Rules:\n"
    "- Identify claims by pet name + Petcover reference, never internal ids.\n"
    "- Use the read tools to answer; summarise, don't dump.\n"
    "- To change anything, call a propose_* tool. It does NOT act — it queues a confirmation the "
    "user must tap. Never claim an action is done; say it's awaiting confirmation.\n"
    "- Never send email (drafts only) and never invent a required field such as a condition. If a "
    "detail is missing, ask for it.\n"
    "- If a target claim is ambiguous or not found, ask the user to clarify. Do not guess.\n"
    "- Never reveal API keys, bank details, or configuration."
)

# ---- data access (explicit safe columns only; no bank/owner/secret fields) ----

_CLAIMS_SQL = """
SELECT vc.id, vc.status, vc.flag, vc.condition_text, vc.petcover_reference, vc.draft_id, vc.pet_id,
       p.name AS pet_name, bt.date AS txn_date, bt.amount AS txn_amount, bt.merchant AS merchant
FROM vet_claims vc
LEFT JOIN pets p ON p.id = vc.pet_id
LEFT JOIN bank_transactions bt ON bt.id = vc.transaction_id
"""


def _find_claims(pet=None, reference=None, status=None, merchant=None, unassigned=False):
    with db.get_connection() as conn:
        rows = conn.execute(_CLAIMS_SQL).fetchall()
    out = []
    for r in rows:
        if pet and pet.lower() not in (r["pet_name"] or "").lower():
            continue
        if reference and reference.lower() not in (r["petcover_reference"] or "").lower():
            continue
        if status and (r["status"] or "") != status:
            continue
        if merchant and merchant.lower() not in (r["merchant"] or "").lower():
            continue
        if unassigned and r["pet_id"] is not None:
            continue
        out.append(r)
    return out


def _label(r) -> str:
    return f"{r['pet_name'] or 'unassigned'} · {r['petcover_reference'] or 'no ref'} · {r['status']}"


def _summary_line(r) -> str:
    amount = f"${abs(r['txn_amount']):.2f}" if r["txn_amount"] is not None else "$?"
    line = f"{_label(r)} · {r['txn_date'] or '?'} {amount} · {r['merchant'] or '?'}"
    if r["condition_text"]:
        line += f" · condition: {r['condition_text']}"
    if r["flag"]:
        line += f" · ⚠ {r['flag']}"
    return line


def _events_summary(claim_id: int) -> str:
    with db.get_connection() as conn:
        events = conn.execute(
            "SELECT event_type, created_at, detail FROM claim_status_events "
            "WHERE claim_id = ? ORDER BY created_at",
            (claim_id,),
        ).fetchall()
    if not events:
        return "  (no Petcover replies recorded)"
    lines = []
    for e in events:
        detail = json.loads(e["detail"] or "{}")
        subject = detail.get("subject", "")
        date = (e["created_at"] or "")[:10]
        lines.append(f"  {date} {e['event_type']}{f' — {subject}' if subject else ''}")
    return "\n".join(lines)


def _single_target(rows):
    """Collapse a batch (claims sharing one draft_id = one submission) to a single
    target. Returns (row, None) when unambiguous, else (None, 'none'|'ambiguous')."""
    if not rows:
        return None, "none"
    draft_ids = {r["draft_id"] for r in rows}
    if len(rows) == 1 or (len(draft_ids) == 1 and None not in draft_ids):
        return min(rows, key=lambda r: r["id"]), None
    return None, "ambiguous"


# ---- tool implementations (closures capture the per-turn proposals list) ----


def _build_impls(proposals: list) -> dict:
    def query_claims(status=None, pet=None):
        rows = _find_claims(pet=pet, status=status)
        if not rows:
            return "No matching claims."
        return "\n".join(_summary_line(r) for r in rows[:25])

    def claim_history(pet=None, reference=None):
        rows = _find_claims(pet=pet, reference=reference)
        if not rows:
            return "No matching claims."
        out = []
        for r in rows[:10]:
            out.append(f"{_label(r)}:")
            out.append(_events_summary(r["id"]))
        return "\n".join(out)

    def _propose(action, rows, arg=None, label=None):
        target, why = _single_target(rows)
        if target is None:
            if why == "none":
                return "No matching claim found. Ask the user to clarify which claim."
            return "Multiple different claims match. Ask the user which one (by pet + Petcover reference)."
        label = label or _label(target)
        proposals.append({"action": action, "claim_id": target["id"], "label": label, "arg": arg})
        return f"Proposed: {action.replace('_', ' ')} for {label}. Tell the user and ask them to tap Confirm."

    def propose_mark_sent(pet=None, reference=None):
        return _propose("mark_sent", _find_claims(pet=pet, reference=reference))

    def propose_set_condition(condition_text, pet=None, reference=None):
        if not condition_text or not condition_text.strip():
            return "No condition text supplied. Ask the user what condition to record — never invent one."
        rows = _find_claims(pet=pet, reference=reference)
        target, why = _single_target(rows)
        label = _label(target) + f" → condition: {condition_text}" if target else None
        return _propose("set_condition", rows, arg=condition_text.strip(), label=label)

    def propose_assign_pet(pet_name, reference=None, merchant=None):
        with db.get_connection() as conn:
            pet = conn.execute("SELECT id, name FROM pets WHERE name = ? COLLATE NOCASE", (pet_name,)).fetchone()
            known = [r["name"] for r in conn.execute("SELECT name FROM pets ORDER BY name")]
        if pet is None:
            return f"No pet named '{pet_name}'. Known pets: {', '.join(known)}."
        rows = _find_claims(reference=reference, merchant=merchant, unassigned=True)
        target, why = _single_target(rows)
        label = f"{_summary_line(target)} → assign {pet['name']}" if target else None
        return _propose("assign_pet", rows, arg=pet["id"], label=label)

    def propose_mark_resolved(pet=None, reference=None):
        return _propose("mark_resolved", _find_claims(pet=pet, reference=reference))

    return {
        "query_claims": query_claims,
        "claim_history": claim_history,
        "propose_mark_sent": propose_mark_sent,
        "propose_set_condition": propose_set_condition,
        "propose_assign_pet": propose_assign_pet,
        "propose_mark_resolved": propose_mark_resolved,
    }


def _fn(name, description, properties, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


_PET = {"type": "string", "description": "pet name (partial ok)"}
_REF = {"type": "string", "description": "Petcover reference (partial ok)"}

TOOLS = [
    _fn("query_claims", "List claims, optionally filtered by status and/or pet, as compact summaries.",
        {"status": {"type": "string", "description": "e.g. pending_match, matched, drafted, sent, acknowledged, "
                    "info_requested, suspended, settled, declined"}, "pet": _PET}),
    _fn("claim_history", "Show a claim's Petcover reply/status-event history, found by pet and/or reference.",
        {"pet": _PET, "reference": _REF}),
    _fn("propose_mark_sent", "Propose marking a drafted claim as sent (starts Petcover reply tracking). "
        "Queues a confirmation; does not act.", {"pet": _PET, "reference": _REF}),
    _fn("propose_set_condition", "Propose setting the condition being claimed for. Queues a confirmation.",
        {"condition_text": {"type": "string", "description": "the condition, supplied by the user"},
         "pet": _PET, "reference": _REF}, required=["condition_text"]),
    _fn("propose_assign_pet", "Propose assigning a pet to an unattributed vet transaction. Queues a confirmation.",
        {"pet_name": {"type": "string"}, "reference": _REF,
         "merchant": {"type": "string", "description": "vet/merchant name to locate the unassigned claim"}},
        required=["pet_name"]),
    _fn("propose_mark_resolved", "Propose confirming an info-request/suspension has been dealt with. "
        "Queues a confirmation.", {"pet": _PET, "reference": _REF}),
]


def handle_message(text: str) -> tuple[str, dict | None]:
    """Run one chat turn. Returns (reply_text, proposed_action_or_None). The
    proposal, if any, is what the Telegram layer turns into a Confirm button."""
    proposals: list = []
    impls = _build_impls(proposals)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
    result = llm.chat(messages, tools=TOOLS, tool_impls=impls, purpose="chat")
    return result["text"], (proposals[-1] if proposals else None)
