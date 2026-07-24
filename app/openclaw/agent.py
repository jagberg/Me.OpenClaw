"""Conversational agent for the Telegram bot: read/act tools over the claims
domain, driven by llm.chat's bounded tool loop.

Read tools run immediately and return compact summaries (never raw email dumps —
keeps turns under the provider's 8k context cap). Act tools NEVER mutate: they
record a *proposed action* that the Telegram layer renders as a Confirm button;
the write happens only on the tap (telegram_bot._execute_action). That harness
gate — not the model's good behaviour — is what enforces the hard rules.
"""
import json

from . import claim_status, db, llm

_BASE_SYSTEM_PROMPT = (
    "You are OpenClaw's assistant for Justin, over Telegram. You help interrogate and act on "
    "pet-insurance claims and their Petcover email replies.\n"
    "Rules:\n"
    # Was "never internal ids" — but Justin acts BY id (/mark 6 …, /pet 1 …), so
    # an answer without ids is unusable. Confirmed live: he asked what was
    # outstanding, got a list with no ids, and could act on none of it.
    "- ALWAYS include the claim id as #N when you mention a claim, plus the amount and vet. "
    "Justin acts by id, so an answer without ids is useless to him.\n"
    "- Use the read tools to answer; summarise, don't dump.\n"
    "- For 'what do I need to do' / 'what's outstanding', call pending_actions. Never assemble "
    "that answer yourself from query_claims — you will miss things.\n"
    "- To change anything, call a propose_* tool. It does NOT act — it queues a confirmation the "
    "user must tap. Never claim an action is done; say it's awaiting confirmation.\n"
    "- Never send email (drafts only) and never invent a required field such as a condition. If a "
    "detail is missing, ask for it.\n"
    "- If a target claim is ambiguous or not found, ask the user to clarify. Do not guess.\n"
    "- Never reveal API keys, bank details, or configuration.\n"
    # Each of these three was a real, observed failure in one morning's chat.
    "- NEVER invent a pet name. The only pets are listed below. If you need a pet and don't know "
    "which, ask using those names — never guess a name and never ask Justin to confirm a name he "
    "did not say.\n"
    "- You CANNOT read Justin's mailbox, search his email, or see what he has sent. If he asks you "
    "to check his sent mail or emails, say plainly that you can't read his mailbox, then offer "
    "reconcile_sent_invoice_requests, which checks Gmail for invoice-request drafts he has since "
    "sent and updates those claims. Never imply you looked at his email.\n"
    "- Never mention tool or function names to Justin. Say what you can do in plain words "
    "('I can mark it sent'), not the name of the tool that does it."
)


def _known_pets() -> list[str]:
    with db.get_connection() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM pets ORDER BY name")]


def system_prompt() -> str:
    """The real pet list is injected rather than left to the model's imagination
    — it hallucinated 'Whiskers' and 'Fluffy' when it had to guess."""
    pets = _known_pets()
    return f"{_BASE_SYSTEM_PROMPT}\nThe ONLY pets that exist: {', '.join(pets) if pets else '(none on file)'}."

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
    # #id first: it's the handle Justin uses for every command.
    return f"#{r['id']} {r['pet_name'] or 'unassigned'} · {r['petcover_reference'] or 'no ref'} · {r['status']}"


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

    def pending_actions():
        """Authoritative 'what does Justin have to do' — the same derivation the
        /actions cards use, so chat and cards can never disagree."""
        actions = claim_status.pending_actions()
        if not actions:
            return "Nothing is waiting on Justin — every claim is with Petcover or closed."
        lines = []
        for a in actions:
            suffix = "" if a["actionable"] else "  [BLOCKED — no action can clear this]"
            who = a["pet_name"] or "no pet yet"
            lines.append(
                f"#{a['claim_id']} {a['title']} — {who} · {a['merchant']} · "
                f"${abs(a['amount']):.2f} · {a['date']} ({a['age_days']}d ago){suffix}"
            )
        return "\n".join(lines)

    def reconcile_sent_invoice_requests():
        """Answers 'go through my sent emails and update the status'. Acts
        directly (rather than proposing) because it only reads Gmail labels and
        records what Justin already did himself — it sends nothing and cannot
        pick a wrong target."""
        from . import pipeline

        try:
            result = pipeline.reconcile_sent_invoice_requests()
        except Exception as exc:  # visible failure, never a silent "all done"
            return f"Couldn't check Gmail: {exc}. Tell Justin it failed."
        if not result["checked"]:
            return "No invoice-request drafts were awaiting confirmation, so nothing to reconcile."
        parts = [f"Checked {result['checked']} invoice-request draft(s)."]
        if result["confirmed_sent"]:
            parts.append("Confirmed sent: " + ", ".join(f"#{i}" for i in result["confirmed_sent"]))
        if result["stale_drafts"]:
            parts.append(
                "These drafts no longer exist in Gmail and need re-sending: "
                + ", ".join(f"#{i}" for i in result["stale_drafts"])
            )
        if not result["confirmed_sent"] and not result["stale_drafts"]:
            parts.append("None had been sent yet.")
        return " ".join(parts)

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
        "pending_actions": pending_actions,
        "reconcile_sent_invoice_requests": reconcile_sent_invoice_requests,
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
    _fn("pending_actions", "THE list of everything waiting on Justin, with claim ids, amounts and age. "
        "Use this for any 'what do I need to do / what's outstanding / what's blocked' question.", {}),
    _fn("reconcile_sent_invoice_requests",
        "Check Gmail for invoice-request drafts Justin has since sent himself and update those claims. "
        "Use when he says he sent emails and wants statuses updated. This is the ONLY way to look at "
        "his mail — you cannot search or read his mailbox.", {}),
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


# Recent turns per chat, so a follow-up like "for aari" still knows what it's
# answering. Turns only — tool call/result payloads are dropped, since they're
# the bulk of the tokens and the provider context is tight. In-memory: a restart
# loses the thread, which is acceptable for chit-chat continuity (a restart also
# drops pending prompts, see telegram_bot._pending_*).
_history: dict[int, list] = {}
HISTORY_TURNS = 6


def handle_message(text: str, chat_id: int | None = None) -> tuple[str, dict | None]:
    """Run one chat turn. Returns (reply_text, proposed_action_or_None). The
    proposal, if any, is what the Telegram layer turns into a Confirm button."""
    proposals: list = []
    impls = _build_impls(proposals)
    prior = _history.get(chat_id, []) if chat_id is not None else []
    messages = [{"role": "system", "content": system_prompt()}, *prior, {"role": "user", "content": text}]
    result = llm.chat(messages, tools=TOOLS, tool_impls=impls, purpose="chat")
    if chat_id is not None:
        turns = [*prior, {"role": "user", "content": text}, {"role": "assistant", "content": result["text"] or ""}]
        _history[chat_id] = turns[-HISTORY_TURNS * 2 :]
    return result["text"], (proposals[-1] if proposals else None)
