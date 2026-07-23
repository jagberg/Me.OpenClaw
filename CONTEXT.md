# OpenClaw Claims

Vet-insurance claims automation for one household: bank charges in, ready-to-send insurer submissions out, reply tracking until settlement.

## Language

**Claim**:
One `vet_claims` row, anchored 1:1 to a bank charge. The system's unit of reconciliation ("claim #22"). Not what the insurer sees.
_Avoid_: transaction-claim, charge-claim

**Submission**:
One send to Petcover: one draft, one filled form, 1–4 invoices. Identified by `draft_id`. Claims sharing a `draft_id` move together. A Submission does NOT own a reference — Petcover files its documents into Condition Threads, possibly several.
_Avoid_: batch, claim (in insurer-facing copy)

**Condition Thread**:
Petcover's actual unit: one (pet, condition) pairing with one claim reference, reused for the life of the condition — proven to span years and many settle cycles (DC1-27-5628 Arthritis: settled Feb 2026, reused Jul 2026). Petcover assigns the condition themselves from the invoices; our condition text is input, not authority. A declined thread is terminal only for its own claims — other threads are unaffected.
_Avoid_: claim reference (the reference is the thread's id, not the thing itself)

**Serial (Sr)**:
Petcover's running number for each claim document inside a Condition Thread ("DC1-27-5628 Sr 3"). Their letters cite reference + Sr; it is how an event targets one claim within a thread.

**Excess**:
$150 deducted from the first settlement of each Condition Thread in each Policy Year. Consumed once per thread per year — a second same-year settlement in that thread must not deduct it again.

**Policy Year**:
Runs anniversary-to-anniversary of the pet's policy, NOT the calendar year. Excess consumption and the $10k annual cap both reset on the anniversary.

**Invoice**:
The vet's per-visit itemised document. Usually paid by one charge; can be paid across several charges (merge), and up to 4 ride one Submission.
_Avoid_: bill, statement (a statement is precisely NOT an invoice — running totals fail adequacy validation)

**Charge**:
A bank transaction from the NetBank CSV. The ceiling on what its Claim can be worth, never the claimed amount itself.
_Avoid_: payment, transaction (ambiguous with Petcover payouts)
