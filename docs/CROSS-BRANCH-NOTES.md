# Cross-branch coordination — `feature/dashboard` ↔ `feature/telegram-claim-query`

Left by the telegram/LLM session (branch `feature/telegram-claim-query`, PR #1) for the dashboard session. No file conflicts between the branches — only `db.py` and `test_core.py` are touched by both, in non-overlapping regions (auto-merge). Two things worth knowing:

## 1. Claim-draft guard changes when a claim reaches `drafted`

`feature/telegram-claim-query` adds a guard in `claim_forms.py`: a Petcover claim is **not** drafted until its itemised invoice is on file (`invoice_file_path`). Until then the claim stays at status **`matched`** with flag `"awaiting itemised invoice from vet — not drafting until it can be attached"`.

Impact for the visit ledger: claims legitimately sit at `matched` (not `drafted`) while waiting on the vet's invoice. Your `visit_ledger` / `_apply_excess_and_cap` already handle the "no invoice yet" case (`claimable is None` → unavailable), so this **aligns** — just confirming the status you'll see reflects the new guard.

## 2. `pets.annual_excess` / `annual_cap` need an explicit migration

Your branch adds these columns to the `pets` CREATE TABLE + `SEED_PETS`. Per CLAUDE.md, `CREATE TABLE IF NOT EXISTS` will **not** add columns to an already-created live DB — the live `pets` table won't get them on startup. Add a migration entry alongside `_migrate_vet_claims_columns` in `db.py` (or a parallel `_migrate_pets_columns`), else expected-reimbursement reads `NULL` on the real DB even though the schema string looks right.

(My branch's `non_vet_merchants` is a new table, so `CREATE IF NOT EXISTS` covers it — no migration needed there.)

## Suggested merge order

Merge `feature/telegram-claim-query` (PR #1) first — it only adds a table + agent/pipeline code. Then rebase `feature/dashboard` on updated master (trivial: the shared regions don't overlap) and add the `pets`-column migration at that point.
