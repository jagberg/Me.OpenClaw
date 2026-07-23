# Tasks — condition-thread-tracking

## 1. Schema + backfill

- [ ] 1.1 Manual live DDL: `ALTER TABLE vet_claims ADD COLUMN petcover_sr INTEGER;` `ALTER TABLE pets ADD COLUMN policy_anniversary TEXT;` — mirror both in `db.py` schema for fresh DBs; add `ops_alerts` table (CREATE IF NOT EXISTS)
- [ ] 1.2 Backfill `petcover_sr` for claims 18/19/21 from the July acks (Sr 2/3/4 mapping, oldest-txn-first)
- [ ] 1.3 Mine renewal emails for Aari's policy anniversary; store on `pets.policy_anniversary` (fallback: ask Justin on Telegram); record what was found

## 2. Event routing (claim_status.py)

- [ ] 2.1 Extract Sr from letters (`Sr\s*N` near the reference, context-phrase style)
- [ ] 2.2 Routing precedence: (reference, Sr) → single claim; reference-only → non-terminal thread claims (shared TERMINAL_STATUSES constant); update `find_claims_by_reference` callers
- [ ] 2.3 Thread isolation: decline events terminal only within their thread (verify no cross-thread status writes)
- [ ] 2.4 Tests: Sr routing, settled-claims-untouched on reference reuse, decline isolation

## 3. Ack correlation (claim_status.py)

- [ ] 3.1 Parse printed condition + Sr from acknowledgement letters
- [ ] 3.2 Replace pet-only pool logic: condition-content match → most-recent-sent fallback → same-day LIFO ordering; within-submission Sr assignment oldest-txn-first
- [ ] 3.3 Recency fallback leaves claim.condition_text untouched when Petcover re-conditioned
- [ ] 3.4 Tests: condition decides, recency fallback, same-day LIFO, re-conditioned document, batch Sr assignment

## 4. Settlement validation (claim_status.py + pipeline notify)

- [ ] 4.1 Expected-payout math: claimable − excess-if-unconsumed (thread + policy year from anniversary), bounded by remaining cap; $2 tolerance; degraded rule when anniversary unknown
- [ ] 4.2 Shortfall → flag `settlement short — expected $X, paid $Y (…)` + Telegram with settlement PDF
- [ ] 4.3 Seed pre-system thread history where known (Feb 2026 arthritis settlement) so excess state starts correct
- [ ] 4.4 Tests: second-settlement-same-year shortfall, anniversary boundary, unknown-anniversary degradation, within-tolerance no-flag

## 5. Gmail auth alerting (pipeline.py)

- [ ] 5.1 Catch RefreshError/missing-token at the Gmail-phase top; skip Gmail steps that tick
- [ ] 5.2 `ops_alerts` state: ≤5 alerts per rolling 24h; restore confirmation once on first success after alerts; state survives restart
- [ ] 5.3 Tests: cap at 5/24h, restore-once, fresh cycle after recovery

## 6. Continuation default (claim_forms.py)

- [ ] 6.1 `process_claim`/`process_claim_batch` pass `continuation=True`; test asserts the form field

## 7. Ship + live verify

- [ ] 7.1 Full suite green; commit; deploy worktree compose rebuild
- [ ] 7.2 Live: next tick processes the 23 Jul Petcover emails (DC1-26-5978 Sr1 + DC1-27-5628 Sr3 letters) — verify routing lands on the right claims and nothing touches settled ones; record results here
