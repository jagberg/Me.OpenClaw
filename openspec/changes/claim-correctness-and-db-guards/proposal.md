## Why

Six open backlog items share one shape: **the system holds a record that may be wrong, and nothing in place proves otherwise or stops it happening again.** Two of them are money — every Petcover serial we hold is on the wrong claim (0 for 10, measured against Petcover's own status table), and "redo claim #N" has been asked twice in live use with no operation behind it. The rest are the guards and assertions that were deferred while those were unknown.

They are grouped because sequencing binds them, not because they are one feature. The serial remap **must** precede the recovery re-read of the five lost approval letters, or that re-read attaches real settlements to wrong claims. The redo decision **also** decides whether `submission-group-id`'s derived `S6+7` token has to become a stored column. Doing either alone leaves the other blocked.

*Scope note, stated rather than hidden:* the host-DB guard and the `_action_kind` assertions are independent of the claims work and could ship as their own change. They are here because they are small, they are the last two unguarded items from the same audit, and splitting them would mean three changes competing for the same review.

## What Changes

- **Correct the live serial → claim map.** Nine claims' `petcover_sr` / `petcover_reference` links are rewritten from the true map in `serial-assignment-by-evidence`'s post-mortem. **Money-affecting live write — gated on Justin's explicit go-ahead, and it runs inside the app container, never from the host.**
- **Define and build "redo claim #N".** Today it falls through to `propose_create_task`, which saves a task and reads as action. Three distinct operations are called redo — rebuild the draft, re-extract the invoice, full reset to `pending_match` — and only Justin can say which the phrase means. **Gated on that answer**; the build follows it.
- **Decide `submission_id` storage as a consequence.** If the chosen redo semantics can re-group drafted claims, the derived `S<a>+<b>` token stops being stable and needs a stored column (manual `ALTER TABLE` + backfill on the live DB). If not, it stays derived and this closes with a recorded "no change".
- **Make ADR-0018's read-only rule mechanical.** A guard that rejects a host-side command opening the live DB without `mode=ro`, plus the `scripts/query_db.py` helper the ADR deferred. Convention has now failed twice; the ADR itself says nothing prevents the next plain `connect()`.
- **Assert `_action_kind` per kind.** Four of nine kinds are asserted anywhere. Add one assertion each for `split_proposal`, `unmatch`, `confirm_resolved`, `invoice_request_sent` — the last two being the pair the extraction actually moved.
- **Three read-only verifications that need no decision**: claim #8's card should now read `$192.72` (the whole point of the 65% benefit rate, never checked); claim #21's $44.75-vs-$35.00 discrepancy; claim #17's vision-OCR retry that stopped after one of three attempts.

**Not in scope, deliberately:** the recovery re-read of the five lost approval letters. It is sequenced *after* the remap and is a separate live write with its own go-ahead. Naming it here as a dependency is not the same as doing it.

## Capabilities

### New Capabilities

None. Every behaviour here belongs to a capability that already exists; inventing one would fragment the baseline rather than describe it.

### Modified Capabilities

- `condition-thread-tracking`: a serial → claim mapping can be **corrected** after the fact, and a correction is admissible only on stated evidence (Petcover's treatment-date-per-serial table), never on ordering. Records that no scheduled feed supplies that table.
- `claim-form-automation`: "redo" becomes a defined operation with one chosen meaning, rather than three meanings and no implementation.
- `conversational-agent`: "redo claim #N" resolves to that operation instead of falling through to task capture — an honest non-action that reads like action is the failure being removed.
- `claims-pipeline-resilience`: host-side access to the live DB is **mechanically enforced** read-only, not conventionally so. Extends ADR-0018 Alternative 4, which was left unbuilt pending recurrence.

## Impact

- **Live DB, money-affecting**: `vet_claims.petcover_sr` / `petcover_reference` on nine claims. Runs inside the app container with a backup and a reviewed dry-run diff — never a host-side write (ADR-0018).
- **Possible schema change**: a `submission_id` column, only if redo semantics demand it. Manual `ALTER TABLE` + backfill; `CREATE TABLE IF NOT EXISTS` will not touch the live table.
- **Code**: `claim_forms.py`, `claim_status.py`, `invoice_matching.unmatch`, the agent's tool surface, `scripts/`, and the test suites (`test_core.py` *and* `test_telegram.py` — both).
- **Decisions this blocks on**: two, both Justin's. Neither is inferable from data we hold.
- **Sequencing this creates**: the five-letter recovery re-read cannot start until the remap lands.
- **Tasks #124 and #125** are open duplicates created by the original redo miss; they close when redo lands.
