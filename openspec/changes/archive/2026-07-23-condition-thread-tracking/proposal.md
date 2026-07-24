# Condition Thread tracking, settlement validation, Gmail-auth alerting

## Why

Mining the full 2022–2026 Petcover history proved the current correlation model wrong: a Petcover claim reference is not per-submission — it is a **(pet, condition) thread** reused for years (DC1-27-5628: settled Feb 2026, reused as Sr 2/3/4 in Jul 2026), with events citing reference + Sr. Today `find_claims_by_reference` would stamp new events onto long-settled claims, no letter citing an Sr can be routed to its claim, and nothing checks a settlement's dollars against the deterministic policy math ($150 excess per condition per policy year, $10k cap). Separately, when the Gmail token dies every Gmail-dependent step fails silently in logs — Telegram (which still works) says nothing. Decisions were grilled and recorded in ADR-0011/0012 and CONTEXT.md.

## What Changes

- **Condition Thread model**: claims store `petcover_sr`; acknowledgements attach a claim to a thread (reference + Sr). Events citing reference + Sr route to that one claim; reference-only events touch only the thread's non-terminal claims (never settled/declined ones). A declined thread never blocks other threads.
- **Ack→claim correlation** (per Justin's rule): match the ack's printed condition + pet against un-referenced submitted claims first; fall back to the most-recently-sent submission; multiple same-day acks map last-ack→last-sent working backwards. Petcover's printed condition wins over our condition_text (they assign conditions themselves).
- **Settlement validation**: expected payout = claimable subtotal − excess (only if the thread's excess is unconsumed this policy year), bounded by the $10k remaining annual cap; policy years run anniversary-to-anniversary. Paid < expected beyond tolerance → flag + Telegram with the settlement PDF.
- **Gmail auth-death alerting**: `RefreshError`/missing-token detected specifically; ≤5 Telegram alerts/day while broken; one "restored" confirmation on recovery.
- **Continuation default**: claim form's continuation box always ticked (ADR-0012).
- Pets gain a policy anniversary date (mined from renewal emails, stored on `pets`).

## Capabilities

### New Capabilities
- `condition-thread-tracking`: thread membership, Sr storage, event routing by (reference, Sr), thread isolation, ack correlation rules.
- `settlement-validation`: expected-vs-paid math, excess consumption per thread per policy year, annual cap ledger, shortfall flagging.
- `ops-alerting`: Gmail auth-failure detection and rate-limited Telegram alerting with recovery confirmation.

### Modified Capabilities
- `claim-status-tracking`: reference correlation requirements change — reference-only events must exclude terminal claims; correlation gains condition-content matching and recency/LIFO fallback (replaces pet-name-only pool matching as the primary rule).
- `claim-form-automation`: continuation field requirement changes from unspecified to default-ticked.

## Impact

- `claim_status.py` (correlation, event routing — the core of the change), `claim_forms.py` (continuation default), `pipeline.py` (auth alert hook, settlement flag notify), `db.py` + manual live DDL (`vet_claims.petcover_sr`, `pets.policy_anniversary`, auth-alert state).
- Docs already written: ADR-0011, ADR-0012, CONTEXT.md glossary (Claim, Submission, Condition Thread, Serial, Excess, Policy Year).
- No new third-party calls; Telegram volume bounded (≤5/day auth alerts).
