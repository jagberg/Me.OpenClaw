"""Conversational agent for the Telegram bot: read/act tools over the claims
domain, driven by llm.chat's bounded tool loop.

Read tools run immediately and return compact summaries (never raw email dumps).
The reason is NOT a context cap — measured 2026-07-25, llama-3.3-70b-versatile
has a 131,072-token context window, so agent.py's old "8k cap" claim was wrong.
The real ceiling is Groq's free-tier **100,000 tokens per DAY** (see config.py).
Every request re-sends the whole tool schema (~1.5k tokens of the ~2.6k a chat
request costs), so the day's budget is well under 40 requests and ONE turn can
spend several. That is why the tool loop stays at the default 4 iterations and
why summaries are mandatory: a turn that dumps raw emails costs Justin the rest
of his day. Act tools NEVER mutate: they
record a *proposed action* that the Telegram layer renders as a Confirm button;
the write happens only on the tap (telegram_bot._execute_action). That harness
gate — not the model's good behaviour — is what enforces the hard rules.
"""
import json
from datetime import datetime, timezone

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
    # Live miss: "what claim emails were sent" fetched the vet invoice-request
    # sweep and answered "nothing to verify" while 5 submissions sat awaiting
    # Petcover. Two different mailings, two different tools.
    "- Two kinds of outbound mail exist and must not be confused. Claims go to PETCOVER — for "
    "anything about those ('what was sent', 'awaiting a response', 'did they reply') use "
    "submissions_awaiting_reply. Invoice requests go to the VET — only those use "
    "reconcile_sent_invoice_requests.\n"
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
    # Narrowed, not lifted: the absolute version existed because the agent once
    # answered "I checked your sent mail" with no such capability. It now has
    # three named sweeps and nothing else — the fabrication risk is unchanged.
    "- You CANNOT browse, search, or read Justin's mailbox, and must never imply you did. What you "
    "CAN do is run three specific checks: reconcile_sent_invoice_requests (did he send the "
    "invoice-request drafts?), rematch_claims (re-run invoice matching for unmatched claims, "
    "optionally just one vet's), and poll_petcover_now (pick up new Petcover replies). When you run "
    "one, say what it actually covered — 'I re-checked the 3 unmatched Bondi Vet claims', never 'I "
    "looked through your email'.\n"
    "- poll_petcover_now only sees mail not processed before. If it finds nothing, say nothing NEW "
    "arrived — never that Petcover hasn't replied. To see what was already recorded, use claim_detail.\n"
    "- For tasks, ALWAYS include the task id as #N — it's how Justin closes them. Never invent an "
    "outcome when closing one; if he didn't say what happened, ask.\n"
    # Live: "remember I need to call the vet..." made it call list_tasks.
    "- 'Remember X' / 'don't let me forget X' / 'add a task' means propose_create_task with X as "
    "the description. Only use list_tasks when he asks what tasks he HAS.\n"
    "- You cannot read OpenClaw's code, specs or docs. You CAN explain a claim's own state from its "
    "flag and recorded replies (claim_detail). If asked how the system works internally, say that's "
    "not something you can see rather than guessing at the implementation.\n"
    "- Never mention tool or function names to Justin. Say what you can do in plain words "
    "('I can mark it sent'), not the name of the tool that does it.\n"
    # Telegram sends these replies as plain text (no parse_mode), so markdown
    # arrives literally. gpt-oss-120b — reachable via the fallback chain —
    # answers with pipe tables by default, which render as unreadable pipes.
    "- Reply in short plain-text lines, one claim or task per line. NEVER use markdown tables, and "
    "don't bother with ** bold ** — this is read in Telegram, where that markup shows up literally."
)


def _known_pets() -> list[str]:
    with db.get_connection() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM pets ORDER BY name")]


def system_prompt() -> str:
    """The real pet list is injected rather than left to the model's imagination
    — it hallucinated 'Whiskers' and 'Fluffy' when it had to guess. Today's date
    goes in for the same reason: without it "July 2025" vs "last July" and every
    other relative date was a guess."""
    pets = _known_pets()
    today = datetime.now(timezone.utc).date()
    return (
        f"{_BASE_SYSTEM_PROMPT}\n"
        f"The ONLY pets that exist: {', '.join(pets) if pets else '(none on file)'}.\n"
        f"Today's date is {today.isoformat()}. Resolve any relative date against it and pass "
        "explicit YYYY-MM-DD ranges to the tools; state the range you used in your answer."
    )

# ---- data access (explicit safe columns only; no bank/owner/secret fields) ----

_CLAIMS_SQL = """
SELECT vc.id, vc.status, vc.flag, vc.condition_text, vc.petcover_reference, vc.draft_id, vc.pet_id,
       p.name AS pet_name, bt.date AS txn_date, bt.amount AS txn_amount, bt.merchant AS merchant
FROM vet_claims vc
LEFT JOIN pets p ON p.id = vc.pet_id
LEFT JOIN bank_transactions bt ON bt.id = vc.transaction_id
"""


def _in_range(txn_date, since, until) -> bool:
    """Inclusive ISO-prefix compare — dates are stored as ISO strings, so a
    lexical compare on the first 10 chars is the date compare, no parsing."""
    day = (txn_date or "")[:10]
    if not day:
        return not (since or until)  # undated claim can't satisfy a range
    if since and day < since[:10]:
        return False
    return not (until and day > until[:10])


def _find_claims(pet=None, reference=None, status=None, merchant=None, unassigned=False,
                 since=None, until=None):
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
        if (since or until) and not _in_range(r["txn_date"], since, until):
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


def _range_label(since, until) -> str:
    if since and until:
        return f" between {since[:10]} and {until[:10]}"
    if since:
        return f" on or after {since[:10]}"
    return f" on or before {until[:10]}" if until else ""


def _build_impls(proposals: list) -> dict:
    def query_claims(status=None, pet=None, merchant=None, since=None, until=None):
        rows = _find_claims(pet=pet, status=status, merchant=merchant, since=since, until=until)
        if not rows:
            # Say the range was empty rather than answering as if unfiltered —
            # a silently-widened range is a wrong answer that reads as a right one.
            return f"No claims with a transaction{_range_label(since, until)}."
        return "\n".join(_summary_line(r) for r in rows[:25])

    def pending_actions(since=None, until=None):
        """Authoritative 'what does Justin have to do' — the same derivation the
        /actions cards use, so chat and cards can never disagree. since/until
        filter here on the claim's own transaction date; the shared derivation
        is left untouched so cards and chat keep agreeing."""
        actions = claim_status.pending_actions()
        if since or until:
            actions = [a for a in actions if _in_range(a["date"], since, until)]
        if not actions:
            scope = _range_label(since, until)
            if scope:
                return f"Nothing waiting on Justin for transactions{scope}."
            return "Nothing is waiting on Justin — every claim is with Petcover or closed."
        lines = []
        for a in actions:
            suffix = "" if a["actionable"] else "  [BLOCKED — no action can clear this]"
            who = a["pet_name"] or "no pet yet"
            # A submission-level action covers several claims. Name every one of
            # them: the representative id alone would hide the rest, and "every
            # claim reference carries its id" is what makes an answer actionable.
            ids = " ".join(f"#{i}" for i in a["claim_ids"])
            group = f"{a['group_id']} ({ids}) " if len(a["claim_ids"]) > 1 else f"{ids} "
            lines.append(
                f"{group}{a['title']} — {who} · {a['merchant']} · "
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

    def rematch_claims(merchant=None, claim_id=None):
        """'Go through the emails from <vet> and see if they can be processed.'

        Acts directly rather than proposing: this is the identical call the
        pipeline makes unattended every 15 minutes, it cannot send anything
        (Gmail is read + drafts only), and a wrong match is reversible with the
        ❌ Wrong invoice button. Per-claim confirmation would also defeat it —
        the request is inherently a sweep over several claims.

        Idempotent, which is what makes direct action safe under at-least-once
        update replay (ADR-0014): only pending_match claims are considered, so a
        claim the first run matched is no longer in the set."""
        from . import invoice_matching, pipeline

        candidates = list(pipeline._pending_claims())
        if claim_id is not None:
            candidates = [c for c in candidates if c["id"] == int(claim_id)]
        elif merchant:
            candidates = [c for c in candidates if merchant.lower() in (c["txn_merchant"] or "").lower()]
        if not candidates:
            scope = f" for '{merchant}'" if merchant else (f" for #{claim_id}" if claim_id else "")
            return f"No claims are awaiting an invoice match{scope}, so there was nothing to re-check."

        matched, still_waiting, failed = [], [], []
        for claim in candidates:
            try:
                if invoice_matching.match_claim(claim):
                    matched.append(claim["id"])
                else:
                    still_waiting.append(claim["id"])
            except Exception as exc:  # one bad claim must not kill the sweep
                failed.append(f"#{claim['id']} ({type(exc).__name__}: {exc})")

        parts = [f"Re-checked {len(candidates)} claim(s) awaiting an invoice match."]
        if matched:
            parts.append("Now matched: " + ", ".join(f"#{i}" for i in matched))
        if still_waiting:
            parts.append("Still no invoice found: " + ", ".join(f"#{i}" for i in still_waiting))
        if failed:
            parts.append("Failed: " + "; ".join(failed))
        return " ".join(parts)

    def poll_petcover_now():
        """Pick up Petcover replies now instead of waiting for the tick.

        Only sees mail not processed before (gmail_ingest._already_processed).
        Re-reading a seen email would risk re-applying a status against the
        append-only event log, so 'nothing new' must never be reported as
        'Petcover hasn't replied' — the reply text says which it is."""
        from . import pipeline

        try:
            result = pipeline.poll_petcover_status()
        except Exception as exc:  # visible failure, never a silent "all clear"
            return f"Couldn't check Petcover mail: {exc}. Tell Justin it failed."
        if not result["checked"]:
            return ("No Petcover emails have arrived that weren't already processed. This only "
                    "checks NEW mail — it does not mean Petcover has never replied.")
        changed = ", ".join(f"#{i}" for i in result["claims_changed"]) or "none"
        return (f"Processed {result['checked']} new Petcover email(s), recording "
                f"{result['events']} event(s). Claims affected: {changed}.")

    def submissions_awaiting_reply():
        """What's been sent to Petcover and whether an answer came back."""
        rows = claim_status.submissions_awaiting_reply()
        if not rows:
            return "Nothing is sitting with Petcover — every submission is settled, declined or closed."
        lines = []
        for s in rows:
            ids = ", ".join(f"#{i}" for i in s["claim_ids"])
            who = s["pet_name"] or "no pet"
            ref = s["reference"] or "no reference yet"
            # 'unclassified' is a real reply we couldn't read, not a status —
            # live output read as though Petcover had answered something.
            if not s["last_event"]:
                answer = "NO reply recorded yet"
            elif s["last_event"] == "unclassified":
                answer = "a reply arrived that we couldn't classify"
            else:
                answer = f"last reply: {s['last_event']}"
            lines.append(
                f"{ids} · {who} · {ref} · {s['status']} · ${s['total_amount']:.2f} · "
                f"{answer} · {s['days_waiting']}d since last activity"
            )
        return "\n".join(lines)

    def claim_detail(claim_id):
        """One claim in full — the 'why is #N like this' answer."""
        detail = claim_status.claim_detail(int(claim_id))
        if detail is None:
            return f"No claim #{claim_id} found."
        lines = [
            f"#{detail['claim_id']} {detail['pet_name'] or 'unassigned'} · {detail['status']}"
            f" · {detail['reference'] or 'no reference'}"
            + (f" Sr{detail['petcover_sr']}" if detail["petcover_sr"] else ""),
            f"charge: {detail['txn_date']} ${abs(detail['txn_amount'] or 0):.2f} at {detail['merchant']}",
            f"condition: {detail['condition_text'] or '(not set)'}",
        ]
        if detail["invoice_amount"] is not None:
            lines.append(
                f"invoice {detail['invoice_number'] or '?'}: ${float(detail['invoice_amount']):.2f}"
                f", claimable ${float(detail['claimable_amount'] or 0):.2f}"
            )
        for item in detail["items"][:10]:
            lines.append(f"  - {item.get('description', 'item')} {item.get('amount', '')}")
        if detail["flag"]:
            lines.append(f"FLAG: {detail['flag']}")
        for e in detail["events"]:
            figures = " ".join(
                f"{k}=${v}" for k, v in e.items()
                if k.endswith("_amount") or k.endswith("_stated")
            )
            lines.append(f"  {e['at'][:10]} {e['event_type']}{' ' + figures if figures else ''}")
        return "\n".join(lines)

    def list_tasks(status=None):
        """Assistant-side tasks. Nothing else in OpenClaw reads this table."""
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, description, status, follow_up_at, outcome FROM tasks "
                "WHERE (? IS NULL OR status = ?) ORDER BY id",
                (status, status),
            ).fetchall()
        if not rows:
            return f"No {status or ''} tasks on file.".replace("  ", " ")
        lines = []
        for r in rows[:25]:
            follow_up = f" · follow up {r['follow_up_at'][:16]}" if r["follow_up_at"] else ""
            outcome = f" · outcome: {r['outcome']}" if r["outcome"] else ""
            lines.append(f"#{r['id']} [{r['status']}] {r['description']}{follow_up}{outcome}")
        return "\n".join(lines)

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

    def propose_create_task(description):
        """Gated rather than immediate for two reasons: it spends an LLM call
        extracting a follow-up date, and a misheard task resurfaces later as a
        false obligation — the worst failure mode for a reminder system."""
        if not description or not description.strip():
            return "No task text supplied. Ask Justin what the task is — never invent one."
        description = description.strip()
        proposals.append({"action": "create_task", "label": f"new task: {description}",
                          "arg": description})
        return f"Proposed new task: {description}. Tell Justin and ask him to tap Confirm."

    def propose_close_task(task_id, outcome):
        if not outcome or not outcome.strip():
            return "No outcome supplied. Ask Justin what happened — never invent an outcome."
        with db.get_connection() as conn:
            task = conn.execute("SELECT id, description, status FROM tasks WHERE id = ?",
                                (int(task_id),)).fetchone()
        if task is None:
            return f"No task #{task_id} found. Ask Justin which task he means."
        if task["status"] == "closed":
            return f"Task #{task['id']} is already closed."
        proposals.append({"action": "close_task", "task_id": task["id"], "arg": outcome.strip(),
                          "label": f"close task #{task['id']}: {outcome.strip()}"})
        return (f"Proposed closing task #{task['id']} ({task['description']}) with that outcome. "
                "Tell Justin and ask him to tap Confirm.")

    return {
        "query_claims": query_claims,
        "pending_actions": pending_actions,
        "reconcile_sent_invoice_requests": reconcile_sent_invoice_requests,
        "rematch_claims": rematch_claims,
        "poll_petcover_now": poll_petcover_now,
        "submissions_awaiting_reply": submissions_awaiting_reply,
        "claim_detail": claim_detail,
        "claim_history": claim_history,
        "list_tasks": list_tasks,
        "propose_mark_sent": propose_mark_sent,
        "propose_set_condition": propose_set_condition,
        "propose_assign_pet": propose_assign_pet,
        "propose_mark_resolved": propose_mark_resolved,
        "propose_create_task": propose_create_task,
        "propose_close_task": propose_close_task,
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
# Descriptions stay one line each on purpose: the whole schema ships in EVERY
# request on a free-tier budget, and test_tools_schema_stays_small guards it.
_SINCE = {"type": "string", "description": "earliest transaction date, YYYY-MM-DD"}
_UNTIL = {"type": "string", "description": "latest transaction date, YYYY-MM-DD"}
_MERCHANT = {"type": "string", "description": "vet/merchant name (partial ok)"}

TOOLS = [
    _fn("query_claims", "List claims filtered by status, pet, vet and/or transaction-date range.",
        {"status": {"type": "string", "description": "e.g. pending_match, matched, drafted, sent, acknowledged, "
                    "info_requested, suspended, approved, settled, declined"},
         "pet": _PET, "merchant": _MERCHANT, "since": _SINCE, "until": _UNTIL}),
    _fn("pending_actions", "THE list of everything waiting on Justin, with claim ids, amounts and age. "
        "Use this for any 'what do I need to do / what's outstanding / what's blocked' question; "
        "pass since/until to scope it to a transaction period.", {"since": _SINCE, "until": _UNTIL}),
    # Named "…invoice_requests" but the model still grabbed it for "what CLAIM
    # emails were sent" (live, 2026-07-25) and answered "nothing to verify"
    # while 5 submissions sat awaiting Petcover. Both descriptions now say who
    # the mail went TO, which is the only thing that separates them.
    _fn("reconcile_sent_invoice_requests",
        "Emails to the VET asking for a missing invoice: check Gmail for those drafts Justin has "
        "since sent. NOT for questions about claims sent to Petcover.", {}),
    _fn("rematch_claims", "Re-run invoice matching now for claims still awaiting an invoice, "
        "optionally just one vet's or one claim. Use for 'go through the emails from <vet>'.",
        {"merchant": _MERCHANT, "claim_id": {"type": "integer"}}),
    _fn("poll_petcover_now", "Pick up NEW Petcover replies now and report which claims changed. "
        "Sees only mail not processed before — never report 'nothing new' as 'no reply exists'.", {}),
    _fn("submissions_awaiting_reply",
        "Claims sent to PETCOVER and whether a reply came back, one entry per submission. Use for "
        "'what claim emails were sent / what's awaiting a response / has Petcover replied'.", {}),
    _fn("claim_detail", "One claim in full by id: invoice items, claimable, flag, and every reply "
        "with its dollar figures. Use for 'why is claim #N like this'.",
        {"claim_id": {"type": "integer"}}, required=["claim_id"]),
    _fn("claim_history", "Show a claim's Petcover reply/status-event history, found by pet and/or reference.",
        {"pet": _PET, "reference": _REF}),
    _fn("list_tasks", "List Justin's non-claim tasks (household admin, follow-ups).",
        {"status": {"type": "string", "description": "open or closed"}}),
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
    _fn("propose_create_task", "Propose saving a non-claim task Justin wants remembered. Queues a "
        "confirmation.", {"description": {"type": "string", "description": "the task, in his words"}},
        required=["description"]),
    _fn("propose_close_task", "Propose closing a task with what actually happened. Queues a "
        "confirmation; never invent the outcome.",
        {"task_id": {"type": "integer"}, "outcome": {"type": "string", "description": "what happened, "
         "supplied by Justin"}}, required=["task_id", "outcome"]),
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
    # Left at the default 4. It was briefly raised to 6 for headroom, before a
    # live 429 revealed a 100k tokens/DAY cap (config.py) that header-only
    # measurement had missed: at ~2.6k tokens a request, extra iterations are
    # charged against a budget of under 40 requests a day. 4 rounds still cover
    # the deepest real path (sweep -> read -> answer) with a spare.
    result = llm.chat(messages, tools=TOOLS, tool_impls=impls, purpose="chat")
    # NOT named `text` — that's this function's user-message parameter, and the
    # history below records it as the user turn.
    reply = result["text"]
    # A fallback model means the primary's daily budget ran out. Say so: a
    # quietly weaker answer is exactly the invisible failure the hard rules
    # forbid, and Justin should weigh a downgraded reply accordingly.
    _base, primary, _key = llm._resolve()
    if result.get("model") and result["model"] != primary:
        reply = f"⚠️ {primary} is out of daily tokens — answered with {result['model']}.\n\n{reply}"
    if chat_id is not None:
        turns = [*prior, {"role": "user", "content": text}, {"role": "assistant", "content": result["text"] or ""}]
        _history[chat_id] = turns[-HISTORY_TURNS * 2 :]
    return reply, (proposals[-1] if proposals else None)
