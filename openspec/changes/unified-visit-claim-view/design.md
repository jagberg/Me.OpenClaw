## Context

The mock (`docs/mockups/dashboard-mock.html`) renders two tables over the same data:

- **Active claims** — `vet_claims` joined to their transaction, keyed by pet, showing condition / ceiling / claimable / status / reference / last-event.
- **Visits — bank charges** — `bank_transactions WHERE vet_flag=1`, keyed by date, showing merchant / pet / charge / invoice-match / note.

The data model already links them one-way: `vet_claims.transaction_id → bank_transactions.id`, with **one charge → 0..N claims**. So the claims table is a subset (every claim has a charge) and the visits table is the superset (a charge can have no claim yet). Rendering both means each claimed visit's charge is printed twice, and a reader reconstructs a join by eye.

The live template (`app/openclaw/templates/index.html`, 166 lines) is simpler than the mock and driven by `main.py::dashboard()`, which runs four parallel status-scoped queries (`needs_pet`, `pending_match`, `matched`, `drafted`) plus `claim_status.dashboard_lists()`. This change is presentation-only: no claim detection, matching, form-fill, status-event, Gmail, or LLM logic changes.

New domain input this turn: **Aari's policy has a $150 excess per condition per year and a $10,000/year claim cap.** The mock's settlement gap ("35% age contribution") was invented; the real gap is non-claimable line items + excess + cap. Excess/cap are per-pet policy attributes not currently stored (`pets` has `policy_number`, `dob`, but no excess/cap fields).

## Goals / Non-Goals

**Goals:**
- One transaction-anchored ledger replacing the two mock tables, no charge shown twice.
- Preserve every column both tables carried (information density was the explicit ask).
- Handle the three shapes cleanly: 1-charge-1-claim (flat row), 1-charge-N-claims (expand), 1-charge-0-claims (no-invoice row).
- Surface real expected-reimbursement math (claimable − excess, capped) instead of fiction, and flag it when inputs are unknown rather than guessing (CLAUDE.md: never guess required fields).

**Non-Goals:**
- No change to how claims are detected, matched, drafted, sent, or status-tracked.
- Not building a full policy-accounting engine (running per-condition excess ledgers across years). Expected reimbursement is a **display estimate**, clearly labelled, not a booked figure.
- Not touching the Needs-action, Email-review-queue, or Settlements sections beyond wiring; they stay distinct.

## Decisions

**D1 — Anchor rows on `bank_transactions`, not `vet_claims`.**
The ledger iterates vet charges and nests claims underneath. This makes the no-invoice charge a natural first-class row (it is a transaction with an empty claim list) rather than an orphan needing a second table. Alternative — anchor on claims and left-pad no-invoice charges — reintroduces the two-population split we are removing.

**D2 — Assemble in a `visit_ledger()` builder in `claim_status.py`, beside `dashboard_lists()`.**
One query: `bank_transactions` (vet_flag=1) LEFT JOIN `vet_claims` LEFT JOIN `pets`, ordered by date desc, then group rows per transaction in Python into `{txn, claims: [...], claim_count}`. Keeps `main.py::dashboard()` thin and replaces the four parallel status queries for this region. Alternative — do the grouping in the Jinja template — buries logic in markup and can't be unit-tested against the real DB (CLAUDE.md working style).

**D3 — Flatten single-claim charges; expand multi-claim charges.**
`claim_count <= 1` renders one flat row (charge + claim fields inline; empty claim → no-invoice state). `claim_count > 1` renders an anchor row (charge + count badge) plus a sub-row per claim. Ceiling lives on the anchor; claimable subtotals live on sub-rows. Native `<details>`/`<tr>` toggle — no JS framework, no new dependency (mock is already static CSS).

**D4 — Expected reimbursement = `max(0, claimable − excess_applied)`, bounded by remaining cap; unavailable when inputs are missing.**
Excess applies once per condition per policy year (first qualifying claim), so the builder needs per-(pet, condition, year) awareness. `excess` and `annual_cap` come from new nullable `pets.annual_excess` / `pets.annual_cap` columns (added via manual `ALTER TABLE` per CLAUDE.md live-schema rule — `CREATE TABLE IF NOT EXISTS` won't alter existing tables). When a pet's excess/cap is NULL, the column renders "—/unavailable", never a guessed number. Alternative — hard-code Aari's $150/$10k as constants — fails the moment a second Petcover pet exists and violates the no-guessing rule for others.

**D5 — Status column absorbs the mock's separate "Invoice" match state.**
The visits table's Matched / No-invoice chip and the claims table's Drafted / Sent / Settled chip are the same lifecycle at different stages. One status column spans it: `no-invoice → matched → drafted → sent → acknowledged → info_requested/suspended → settled/declined`, plus `blocked` (e.g. Echo, insurer process undefined).

## Risks / Trade-offs

- **Per-condition-per-year excess needs grouping the builder doesn't do today** → keep it a display estimate; compute excess-applied from the claims already in hand for that pet+condition+year, and label the figure "est." Don't block the row if the computation is uncertain.
- **New `pets` columns require a manual live ALTER** → document the exact `ALTER TABLE pets ADD COLUMN ...` in tasks; nullable so existing rows and the smoke DB keep working; missing values flag as unavailable by design.
- **Expanding rows without JS** → `<details>` inside a table is workable but styling is fiddly; acceptable given no-dependency constraint. If it fights the layout, fall back to always-expanded sub-rows (density over compactness).
- **Echo has no Petcover policy** (Bow Wow Insurance, process undefined) → excess/cap columns stay NULL for Echo and correctly render unavailable; the row still shows charge + blocked status.

## Migration Plan

1. Add nullable `pets.annual_excess`, `pets.annual_cap`; mirror in `db.py` schema (for fresh DBs) and run manual `ALTER TABLE` against `app/data/openclaw.db`. Seed Aari = 150 / 10000; leave Echo NULL.
2. Add `visit_ledger()` to `claim_status.py` with an assert-based check in `tests/test_core.py` (flat / split / no-invoice / missing-excess shapes).
3. Swap the two-table region of `index.html` for the ledger; wire `main.py::dashboard()` to pass `visit_ledger()`, drop the now-unused parallel status queries for that region.
4. Verify against the real DB read-only before declaring done (CLAUDE.md).

**Rollback:** revert the template + route; `visit_ledger()` and the new columns are additive and inert if unreferenced.

## Open Questions

- Should expected reimbursement account for reimbursements already **settled** this year (true remaining cap), or just flag the estimate and defer running-total accounting? Lean: flag as est. now, defer accounting.
- Do other Petcover pets exist beyond Aari that need excess/cap seeded, or is Aari the only Petcover policy today?
