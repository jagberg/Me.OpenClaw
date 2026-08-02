"""The one commit path for a proposed mutation, and the durable record of one.

Every claim mutation the model can reach is a *proposal*: it changes nothing
until Justin taps Confirm. That guarantee used to live in
`telegram_bot._execute_action`, which is being deleted with the rest of that
module at the gateway cutover — so the switch moved here, unchanged, and
`telegram_bot` now delegates to it. Both callers run the same code today, which
is the only way the behaviour survives the deletion intact.

**One commit path, decided 2026-08-02.** ADR-0025 originally split the gate by
origin: card taps through `/internal`, chat proposals inside the MCP surface.
The MCP half turned out to have no mechanism — the gateway's MCP client declares
no `elicitation` capability, so a server there cannot ask for a confirmation
mid-call (ADR-0025's 2026-08-02 amendment, and ADR-0027). Both origins now reach
`commit()`, and the entry point is the only difference between them.

Two properties this module exists to hold, both of which outlived the split:

* **A commit is never the return value of a tool the model called.** `record()`
  is what a `propose_*` tool reaches; `commit()` is reachable only from a
  confirm tap. Nothing in the MCP inventory can call `commit()` — asserted, not
  arranged by convention.
* **The confirmation text is composed by code from the row about to change**,
  never from the model's description of what it intends. A model that resolved
  the wrong claim would describe the wrong claim convincingly, which turns an
  approval into a rubber stamp with better typography.
"""

import json
import logging
from datetime import datetime, timezone

from . import claim_forms, claim_status, db, llm

logger = logging.getLogger(__name__)

# Every action `execute` knows. Enumerated rather than derived so the MCP
# inventory test can assert that no propose_* tool reaches a verb this does not
# implement, and so an unknown action is a named failure instead of a no-op.
ACTIONS = ("mark_sent", "set_condition", "assign_pet", "mark_resolved",
           "split_pets", "create_task", "close_task")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(action: str, *, label: str, claim_id=None, task_id=None, arg=None,
           origin: str = "chat") -> int:
    """Persist a proposal and return its id. Commits nothing.

    The id is what a confirm button carries, so it must stay short: a `command`
    button's whole payload is 58 UTF-8 bytes (Telegram's 64 less the gateway's
    `tgcmd:` prefix), and an overflowing button is deleted from the keyboard
    with no error at all.
    """
    if action not in ACTIONS:
        # Loud, not stored. A proposal nothing can execute would sit in the
        # table looking pending forever and its button would do nothing.
        raise ValueError(f"unknown proposal action: {action!r}")
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO pending_proposals (origin, action, claim_id, task_id, arg, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (origin, action, claim_id, task_id,
             None if arg is None else json.dumps(arg), label, _now()),
        )
        return int(cur.lastrowid)


def get(proposal_id: int) -> dict | None:
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM pending_proposals WHERE id = ?", (int(proposal_id),)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["arg"] = None if out["arg"] is None else json.loads(out["arg"])
    return out


def commit(proposal_id: int) -> dict:
    """Run a confirmed proposal. The ONLY caller is a confirm tap.

    Returns `{"ok": bool, "message": str}` rather than raising, because both
    callers put the message straight in front of Justin and a traceback is not
    an answer he can act on.
    """
    proposal = get(proposal_id)
    if proposal is None:
        return {"ok": False, "message": f"Proposal #{proposal_id} not found — nothing was changed."}
    if proposal["confirmed_at"]:
        # Single-use. Telegram redelivers, and a double tap on a mark-sent would
        # be a second Petcover submission for one set of invoices.
        return {"ok": False,
                "message": f"Already confirmed at {proposal['confirmed_at']}: {proposal['result']}"}
    try:
        message = execute(proposal)
    except Exception as exc:  # noqa: BLE001 — visible failure, never a silent no-op
        logger.error("proposal #%s (%s) failed: %s", proposal_id, proposal["action"], exc, exc_info=True)
        # Deliberately NOT stamped confirmed: the write did not happen, so the
        # proposal is still open and the tap can be retried.
        return {"ok": False, "message": f"Couldn't apply that — {exc}. Nothing was changed."}
    with db.get_connection() as conn:
        conn.execute("UPDATE pending_proposals SET confirmed_at = ?, result = ? WHERE id = ?",
                     (_now(), message, int(proposal_id)))
    logger.info("proposal #%s committed (%s): %s", proposal_id, proposal["action"], message)
    return {"ok": True, "message": message}


def execute(proposal: dict) -> str:
    """Run a confirmed mutation through the same domain functions the slash
    commands use — the write happens here, only after a Confirm tap.

    Moved verbatim from `telegram_bot._execute_action` on 2026-08-02 so the two
    entry points cannot drift; that function now delegates here.
    """
    action, claim_id, arg = proposal["action"], proposal.get("claim_id"), proposal.get("arg")
    if action == "mark_sent":
        return claim_status.mark_sent(claim_id)["message"]
    if action == "set_condition":
        return claim_forms.set_condition_text(claim_id, arg)["message"]
    if action == "assign_pet":
        return claim_forms.assign_pet(claim_id, arg)["message"]
    if action == "mark_resolved":
        return claim_status.confirm_resolved(claim_id)["message"]
    # Named split_pets, not split: `split:` callbacks are the per-item CONDITION
    # split, a different axis on the same invoice.
    if action == "split_pets":
        # A JSON round trip turns the shares' tuples into lists; claim_forms
        # unpacks pairs either way, but normalise so the two entry points hand
        # it the identical shape.
        return claim_forms.split_between_pets(claim_id, [tuple(s) for s in arg])["message"]
    # Assistant side — no claim_id involved anywhere in the round trip. Imported
    # here, not at module scope: tasks -> reminders -> scheduler constructs a
    # jobstore against the DB at import time, and neither caller should trigger
    # that just by importing this module.
    from . import tasks

    if action == "create_task":
        try:
            task_id = tasks.create_task(arg, source="chat")
        except llm.LLMUnavailableError as exc:  # visible, never a silent drop
            return f"⚠️ Couldn't save the task — {exc}"
        return f"Task #{task_id} saved: {arg}"
    if action == "close_task":
        tasks.record_outcome(proposal["task_id"], arg)
        return f"Task #{proposal['task_id']} closed: {arg}"
    return f"Unknown action: {action}"
