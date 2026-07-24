# Design — excess-threshold-accrual

## Context

Petcover applies a $150 fixed excess per condition per policy year (ADR-0011; Aari anniversary 09-23 stored). A claim whose claimable subtotal is below the excess pays $0 and returns a "Claim assessment outcome: Under excess" letter (real, confirmed live: "Less Fixed excess: $105.00 / Outstanding excess: $-49.26"). OpenClaw currently drafts every matched claim and treats that letter as a decline, discarding the invoice. It should instead hold submission until a condition's accrued claimable for the CURRENT policy year exceeds $150.

**Superseding note (2026-07-24, ADR-0013 amended):** a live 2-day audit of the claims + Petcover comms surfaced that our year-bucketing must key off each claim's OWN transaction date (not "now"), and that our history for any already-CLOSED policy year is presumed incomplete — so the accrual gate only ever applies to the current, open policy year; a claim whose own transaction falls in a closed prior year assumes the threshold already passed and drafts immediately. The `below_excess`/`approved` classification, the transaction-date bucketing, and this closed-year default were all built and shipped ahead of this change, as a hotfix to `claim_status._validate_settlement` (which had the same "now"-based bucketing bug) — this change's remaining scope is narrower than originally designed: the submission gate, auto-roll, and expiry alert, reusing that already-shipped groundwork rather than re-implementing it.

## Goals / Non-Goals

**Goals:**
- Never draft a condition's claim until its accrued claimable for the policy year exceeds $150.
- Track below-excess replies distinctly; keep their invoices accruing (never terminal, never discarded).
- On release, draft all held + previously-below-excess claims of the condition together (≤4/form), Justin sends.
- Alert before the anniversary when below-excess claimable would expire un-claimable.

**Non-Goals:**
- No auto-send (drafts only, CLAUDE.md).
- No new schema — `below_excess` is a status string; holding is `matched` + flag.
- No percentage-excess handling (real letters show "Less Percentage Excess $0.00 [0%]" — always 0 for this policy).
- No change to how claimable is computed (ADR-0007 line-item subtotal stands).

## Decisions

1. **Accrual key = `(pet_id, condition_text, current policy year)`.** condition_text is Justin-assigned and already a draft prerequisite, so the gate slots in after condition is known. Case-insensitive, trimmed match groups a condition's claims. Policy year determined per claim by **its own transaction date** via the shipped `_policy_year_start`, not "now" — reuse that helper directly rather than a second implementation.
2. **Closed-year bypass (Justin's rule, shipped in the hotfix, reused here as-is):** a claim whose own transaction date falls in an already-CLOSED policy year is presumed to have already passed the threshold — our history there is incomplete (some spend never hits the tracked card; bank-CSV coverage doesn't reach arbitrarily far back). Such a claim is **not** pooled into the accrual gate at all — it drafts immediately, same as an over-threshold current-year claim.
3. **Accrual pool** (current-year claims only) for a key = claimable of all its claims NOT in a terminal state — `matched` (held), `below_excess`, and in-flight `sent`/`acknowledged`/`info_requested`/`suspended`/`approved` — whose OWN transaction date falls in the current open policy year. Sum uses the same claimable subtotal the form carries (`invoice_data.claimable_amount` → `amount`).
4. **Gate** (`accrued_over_excess(pet_id, condition, txn_date)`): for a closed-year claim, always True (decision 2). For a current-year claim: True when the thread already has an `approved`/`settled` sibling whose own transaction date is *also* in the current policy year (excess already used this year → submit freely) OR the current-year pool sum **> $150** (strictly — at exactly $150 payout is $0, per Justin). Otherwise the claim stays `matched` with flag `holding — <condition> accrued $X of $150 excess; waiting for more invoices`. No cumulative/partial-excess modeling — same simple $150-once check the hotfix uses, not a mirror of Petcover's own internal math.
5. **`below_excess`/`approved` classification: already shipped**, ahead of this change, as part of the `claim_status._validate_settlement` hotfix — keyed on the confirmed-live phrase `"Claim assessment outcome: Under excess"` (more reliable than the originally-guessed "under your fixed excess", though both are checked). Neither is in `TERMINAL_STATUSES`; `below_excess` retains the invoice/`invoice_data` so it keeps accruing. Nothing left to do here.
6. **Auto-roll on release** (`_draft_matched_claims`): when the gate opens for a condition, collect that condition's `matched` claims *and* re-open its `below_excess` claims (they already have invoices) into the ready pool, then batch by ≤4 as today. Re-drafting a `below_excess` claim moves it back through `drafted`→`sent`. Deterministic order (txn date, id).
7. **Expiry alert** (`pipeline`): within `EXCESS_EXPIRY_ALERT_DAYS` (30) before a pet's anniversary, for each `(pet, condition)` whose CURRENT-year pool sum is `> 0` and `<= 150` (will never pay this year), send one Telegram alert; dedupe via `ops_alerts` (kind `excess_expiry:<pet>:<condition>:<policy_year>`), reset each new policy year.
8. **No schema change.** `below_excess`/`approved` are status values (already added); `ops_alerts` already exists.

## Risks / Trade-offs

- [In-flight claims counted in accrual] a `sent` claim's real claimable is trusted before Petcover rules on it; if Petcover disallows items the accrual could be optimistic → worst case we submit slightly early and get a below-excess reply, which simply re-holds. Fail-safe.
- [Condition text mismatch] a typo/renamed condition splits accrual across two keys → under-counts, holds longer. Correctable by editing condition_text (existing dashboard/Telegram action); logged holding flag makes it visible.
- [Anniversary unknown] a claim can't be placed in a policy year at all — degrade to always-bypass (draft immediately), same handling as a closed year, rather than guessing a boundary.
- [Re-drafting a below_excess claim] must not double-count or lose its `matched_email_id`/invoice; re-uses the existing invoice_file_path (never re-extracts).
- [Closed-year bypass could submit early] if our incomplete history actually understates a closed year's true accrual, we might submit a claim Petcover still rejects as below-excess — acceptable per Justin: better to try and get re-held (invoice retained either way) than wait forever on data we know is incomplete.

## Migration Plan

1. Code + hermetic tests on `fix/email-matching-gaps` worktree; deploy via compose rebuild. Classification/bucketing groundwork already shipped (hotfix) — this change adds only the submission gate, auto-roll, and expiry alert.
2. No DDL. On deploy, confirm the 2 real below-excess claims already carry `below_excess`/correct classification from the hotfix's live reprocessing; if not, a one-off reclassification. Record which claims.
3. Verify: the held arthritis invoices accrue (current policy year only); closed-year claims (if any) draft immediately without waiting; nothing else drafts until the current-year pool exceeds $150; a synthetic over-threshold case releases a batch.

## Open Questions

- Exact `below_excess` phrase stability across Petcover template versions — mitigated by matching the confirmed-live phrase plus the earlier guess, falling back to `unclassified` (never silent) if both drift.
