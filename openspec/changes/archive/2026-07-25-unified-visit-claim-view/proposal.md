## Why

The dashboard mock shows a vet visit twice: once in **Visits — bank charges** and again in **Active claims**. They are the same object — a `bank_transaction` and the `vet_claims` derived from it (`vet_claims.transaction_id → bank_transactions.id`, one charge → 0..N claims). Reading a single visit means cross-referencing two tables by date and merchant, and a charge with no claim yet (no invoice) lives in a different table than the claim it will become. Merging them into one transaction-anchored ledger keeps every column both tables carried while removing the duplication and the mental join.

## What Changes

- Replace the two separate mock tables (**Active claims**, **Visits — bank charges**) with a **single visit ledger** where each row is anchored on a `bank_transaction` (the charge, the ADR-0007 ceiling) and carries the claim(s) derived from it.
- Flatten the common 1-charge-1-claim case onto one row; **expand** a charge that split into multiple claims (multi-pet / multi-invoice) into sub-rows per claim, ceiling on the anchor, claimable subtotals summing beneath it.
- Represent a charge with **no claim yet** (no invoice) as a first-class row in the same ledger, not a separate table — carrying its "needs manual retrieval" state and invoice-request action.
- Add an **expected reimbursement** column derived from claimable subtotal minus the per-condition annual excess, bounded by remaining annual cap — surfacing what the settlement math actually is (Aari: $150 excess per condition/year, $10k/year cap), replacing the mock's invented "35% age contribution".
- Keep **Needs your action**, **Email review queue**, and **Settlements — claimed vs paid** as their own sections — this change merges only the two visit/claim tables.

## Capabilities

### New Capabilities
- `dashboard-visit-ledger`: The transaction-anchored visit ledger that unifies bank charges and their derived claims into one view, including charge-with-no-claim rows, split-claim expansion, and the excess/cap-aware expected-reimbursement column.

### Modified Capabilities
<!-- No claim-form-automation / claim-status-tracking / invoice-matching REQUIREMENTS change. This is a presentation capability over their existing data; their spec-level behavior is unchanged. -->

## Impact

- **Template**: `app/openclaw/templates/index.html` — the two-table region becomes one ledger table with row expansion.
- **Route**: `app/openclaw/main.py` `dashboard()` — replace the parallel `pending_match` / `matched` / `drafted` / `needs_pet` query set and the separate visits list with one transaction-anchored assembly (`bank_transactions WHERE vet_flag=1` LEFT JOIN `vet_claims`, grouped per charge).
- **New read logic**: a `visit_ledger()` builder (likely in `claim_status.py` alongside `dashboard_lists()`) that groups claims under their charge and computes expected reimbursement.
- **Domain data**: per-pet excess and annual cap are policy attributes not currently stored (`pets` has `policy_number`, `dob` but no excess/cap). Expected-reimbursement needs these — either add `pets.annual_excess` / `pets.annual_cap` columns or treat them as display-only constants for now.
- **No change** to claim detection, invoice matching, form filling, or status-event logic. No Gmail, no LLM, no send path touched.
- Mock `docs/mockups/dashboard-mock.html` is the design reference, not shipped.
