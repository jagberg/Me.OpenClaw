# Design — excess-threshold-accrual

## Context

Petcover applies a $150 fixed excess per condition per policy year (ADR-0011; Aari anniversary 09-23 stored). A claim whose claimable subtotal is below the *remaining* excess pays $0 and returns a "below your fixed excess" letter (real: "Less Fixed excess: $150.00 / Outstanding excess: $-105.25"). OpenClaw currently drafts every matched claim and treats that letter as a decline, discarding the invoice. The excess/policy-year math already lives in `claim_status._validate_settlement` / `_policy_year_start`; this change reuses it for pre-submission gating and adds a distinct below-excess outcome. See ADR-0013 for the decision record (why strict `>`, why auto-roll, why the 2 live below-excess claims are a reclassification and not a reversed decision).

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

1. **Accrual key = `(pet_id, condition_text, policy_year)`.** condition_text is Justin-assigned and is already a draft prerequisite, so the gate slots in after condition is known. Case-insensitive, trimmed match groups a condition's claims. Policy year from `pets.policy_anniversary` via the existing `_policy_year_start`; unknown anniversary → degrade to lifetime (no year boundary), same spirit as settlement-validation.
2. **Accrual pool** for a key = claimable of all its claims NOT in a terminal state and NOT settled — i.e. `matched` (held), `below_excess`, and in-flight `sent`/`acknowledged`/`info_requested`/`suspended`. Sum uses the same claimable subtotal the form carries (`invoice_data.claimable_amount` → `amount`).
3. **Gate** (`accrued_over_excess(pet_id, condition, on_date)`): returns True when the thread already had a settled claim this policy year (excess consumed → submit freely) OR the pool sum **> $150** (strictly — at exactly $150 payout is $0, per Justin). Only then may the condition's `matched` claims draft. Otherwise they stay `matched` with flag `holding — <condition> accrued $X of $150 excess; waiting for more invoices`.
4. **`below_excess` classification**: a new entry in `SUBJECT_KEYWORDS`/body match keyed on the distinctive phrase "under your fixed excess" (body — the subject is the generic "Acknowledgement Letter"). Recorded as event_type `below_excess`; status set to `below_excess`; NOT added to `TERMINAL_STATUSES`; the invoice/`invoice_data` is left intact so it keeps accruing. Ordering in `SUBJECT_KEYWORDS` matters: check `below_excess` before `acknowledged`, since these letters carry the acknowledgement subject.
5. **Auto-roll on release** (`_draft_matched_claims`): when the gate opens for a condition, collect that condition's `matched` claims *and* re-open its `below_excess` claims (they already have invoices) into the ready pool, then batch by ≤4 as today. Re-drafting a `below_excess` claim moves it back through `drafted`→`sent`. Deterministic order (txn date, id).
6. **Expiry alert** (`pipeline`): within `EXCESS_EXPIRY_ALERT_DAYS` (30) before a pet's anniversary, for each `(pet, condition)` whose pool sum is `> 0` and `<= 150` (will never pay this year), send one Telegram alert; dedupe via `ops_alerts` (kind `excess_expiry:<pet>:<condition>:<policy_year>`), reset each new policy year.
7. **No schema change.** `below_excess` is a status value; `ops_alerts` already exists.

## Risks / Trade-offs

- [In-flight claims counted in accrual] a `sent` claim's real claimable is trusted before Petcover rules on it; if Petcover disallows items the accrual could be optimistic → worst case we submit slightly early and get a below-excess reply, which simply re-holds. Fail-safe.
- [Condition text mismatch] a typo/renamed condition splits accrual across two keys → under-counts, holds longer. Correctable by editing condition_text (existing dashboard/Telegram action); logged holding flag makes it visible.
- [Anniversary unknown] degrades to lifetime accrual — only affects Echo (no claim process anyway).
- [Re-drafting a below_excess claim] must not double-count or lose its `matched_email_id`/invoice; re-uses the existing invoice_file_path (never re-extracts).

## Migration Plan

1. Code + hermetic tests on `fix/email-matching-gaps` worktree; deploy via compose rebuild.
2. No DDL. On deploy, reclassify the 2 existing below-excess claims: they currently sit as `acknowledged`/`declined` — re-run status on their letters (or a one-off UPDATE to `below_excess`) so they enter the accrual pool. Record which claims live.
3. Verify: the held arthritis invoices accrue; nothing drafts until the pool exceeds $150; a synthetic over-threshold case releases a batch.

## Open Questions

- Exact `below_excess` phrase stability across Petcover template versions — mitigated by matching the distinctive "under your fixed excess" clause and falling back to `unclassified` (never silent) if it changes.
