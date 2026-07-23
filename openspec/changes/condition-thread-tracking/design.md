# Design — condition-thread-tracking

## Context

Correlation today: `find_claims_by_reference` (all claims with the ref, any status) + `find_claims_by_pet` (pet-name pool, refuses cross-submission ambiguity). Proven wrong against 2022–2026 history: references are (pet, condition) threads reused for years; letters cite reference + "Sr N"; acks print pet ("Ari" misspelling — nickname table exists), condition, reference, Sr — but no amounts/dates. Policy math is deterministic and currently unchecked. Gmail auth death is invisible on Telegram. All decisions pre-agreed in a grilling session: ADR-0011 (threads, routing, correlation rules, isolation), ADR-0012 (continuation), CONTEXT.md glossary.

## Goals / Non-Goals

**Goals:**
- Route every Petcover letter to the right claim(s); never disturb terminal claims.
- Learn reference + Sr per claim at acknowledgement using condition-content → recency → same-day-LIFO rules.
- Validate settlements against expected payout (excess per thread per policy year, $10k cap, anniversary reset).
- Make Gmail auth death loud on Telegram (≤5 alerts/day) with a recovery confirmation.
- Continuation box ticked by default.

**Non-Goals:**
- No thread table UI / dashboard work (flags surface through existing lists).
- No auto-dispute of short settlements — flag + Telegram only; Justin emails Petcover himself.
- No refund/credit handling (rejected: never happens).
- No Bow Wow support (still `claim_process_defined=0`).
- No thread-derived continuation yet (ADR-0012 records it as successor).

## Decisions

1. **No new thread table.** A thread is identified by `petcover_reference`; membership is `vet_claims.petcover_reference`, document identity is new column `vet_claims.petcover_sr` (INTEGER, nullable). Thread state (excess consumed this policy year) is *derived* from settled claims' events at validation time, not stored — avoids a second ledger that can drift. Alternative (threads table with cached excess state) rejected: one more thing to migrate and reconcile; the event log already holds the facts (ADR-0008 spirit).
2. **Routing precedence** in `process_reply`: (a) reference + Sr in the letter → the single claim with that (reference, sr); (b) reference only → claims with that reference whose status is non-terminal (NOT settled/declined); (c) no reference → ack correlation (below). Terminal statuses constant shared with notify code.
3. **Ack correlation** (`find_claims_by_pet` replaced): candidates = submitted-status, un-referenced claims for the printed pet (nicknames). Filter by printed condition == claim.condition_text (case-insensitive, trimmed) when both exist; if exactly one submission's claims survive → attach. Else fall back to the most recent `invoice_request_sent_at`/sent submission (draft_id max sent time); several acks same day → sort acks by received time, map newest ack to newest-sent submission, backwards. Within a multi-claim submission, individual acks (one Sr each) attach to claims oldest-txn-first in Sr order — deterministic, correctable via existing manual tooling. Petcover's printed condition may differ from ours (they re-condition documents): when it matches NOTHING, correlation still proceeds on recency per Justin's explicit rule, and the claim's condition_text is left untouched (theirs is authority in *their* letters only).
4. **Settlement validation** in `claim_status` settlement handler: expected = claimable_amount − (excess if no prior settled claim in this thread within the current policy year) bounded by remaining cap (sum of paid settlements for the pet this policy year). Tolerance $2. Short → `vet_claims.flag = "settlement short — expected $X, paid $Y (…reason…)"` + notify with settlement PDF (existing `_review_pdf`-style attach). Policy year from new `pets.policy_anniversary` (TEXT, MM-DD); mined from renewal emails during implementation, else asked via Telegram.
5. **Auth alerting**: `pipeline.run_once` catches `google.auth.exceptions.RefreshError` + the missing-token `RuntimeError` at the top of the Gmail-touching phase; sends alert if fewer than 5 sent in the trailing 24h (state = tiny `ops_alerts` table: kind, sent_at); on the first successful Gmail call after any alert, sends "restored" once and clears. Alternative (module-level state) rejected: container restarts would re-spam.
6. **Continuation**: `process_claim`/`process_claim_batch` pass `continuation=True` (was None). One line each + form assertion in tests.
7. **Live DDL** (manual, per CLAUDE.md): `ALTER TABLE vet_claims ADD COLUMN petcover_sr INTEGER;` `ALTER TABLE pets ADD COLUMN policy_anniversary TEXT;` — `ops_alerts` is a new table (CREATE IF NOT EXISTS suffices).

## Risks / Trade-offs

- [Sr→claim mapping inside a batch is order-assumed] → deterministic rule + existing unmatch/manual override; wrong mapping only mislabels which twin claim a letter cites, never crosses threads.
- [Same-day LIFO heuristic misroutes] → correction via manual tooling; logged detail keeps the raw letter linked (raw_email_id) for audit.
- [Derived excess state misses pre-system settlements] (e.g. Feb 2026 arthritis settlement predates tracking) → seed: implementation backfills known historical settlements per thread from the mined email history where needed, else first-year expectations may over-estimate by $150 — flag text names the assumption so Justin can dismiss.
- [policy_anniversary unknown] → validation degrades gracefully: without it, skip excess-timing logic and validate only against cap-free expected = claimable − excess-if-thread-never-settled; flag wording says anniversary unknown.

## Migration Plan

1. Code + tests on `fix/email-matching-gaps` (or follow-up branch), deploy via worktree compose rebuild.
2. Manual DDL against live DB before deploy (columns are additive/nullable — old code unaffected; rollback = ignore columns).
3. Backfill: set `petcover_sr` where known (18/19/21 ↔ Sr 2/3/4 mapping from July acks), `pets.policy_anniversary` from renewal email.

## Open Questions

- Renewal email may not state the anniversary explicitly — fallback is asking Justin one Telegram question during rollout.
