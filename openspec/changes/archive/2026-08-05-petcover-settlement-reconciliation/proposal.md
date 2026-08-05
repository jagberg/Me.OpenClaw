## Why

Justin was shown a Telegram card reading `REVIEW SETTLEMENT / Claim #2 … $585.39 / Blocks: a
paid-vs-expected difference is unreviewed`, could not tell what it wanted, and tapped Dismiss. The
card was right that something was wrong and wrong about almost everything else, and dismissal
deleted the only surface it had.

Five real Petcover approval letters were re-read read-only for this proposal (2026-08-04). They
settle three things:

1. **Routing was not the defect.** Every one of the five letters routed to exactly the claim whose
   stored `petcover_reference` and `petcover_sr` it cites. ADR-0020's mis-routing failure mode was
   the working hypothesis going in; the letters refute it. Verified pairs — letter (reference,
   Treatment number) → claim: `(DC1-27-5628, 2)`→#21, `(DC1-27-5628, 5)`→#7, `(DC1-27-5628, 6)`→#6,
   `(DC1-26-5992, 1)`→#8, `(DC1-26-5992, 2)`→#2.

2. **Our expectation formula cannot ever match, because it ignores the letter.** Every approval
   letter states four figures. In all five, Petcover's arithmetic is exact and self-consistent:

   | claim | letter states claimed | fixed excess | age contribution | paid | 0.65 × (claimed − excess) |
   |---|---|---|---|---|---|
   | #21 | $35.00 | $0.00 | $12.25 [35%] | $22.75 | $22.75 |
   | #7 | $446.50 | $49.26 | $139.03 [35%] | $258.21 | $258.21 |
   | #6 | $45.00 | $0.00 | $15.75 [35%] | $29.25 | $29.25 |
   | #8 | $351.50 | $150.00 | $70.53 [35%] | $130.97 | $130.97 |
   | #2 | $35.00 | $0.00 | $12.25 [35%] | $22.75 | $22.75 |

   `_validate_settlement` instead computes `claimable − $150` and compares that to `paid`, so it
   flags a mismatch on a letter whose own numbers add up perfectly. Claim #8's live flag says
   *expected $296.50, Petcover paid $130.97*; against the letter's own stated $351.50 claimed and
   $150.00 excess, $130.97 is correct to the cent. The flag is noise sitting on top of a real
   finding, which is how the real finding became invisible.

3. **The real anomaly is a different one, and only Petcover can answer it.** What does not
   reconcile is `claimed_amount` versus what we submitted. Petcover assessed $446.50 under
   `DC1-27-5628 Sr 5` (our claim #7, invoice $132.50), $351.50 under `DC1-26-5992 Sr 1` (our claim
   #8, invoice $446.50), and $35.00 under `DC1-26-5992 Sr 2` (our claim #2, invoice $580.74). Every
   figure they state matches some real invoice of ours exactly — so they hold our documents; what
   disagrees is which document sits under which serial. Amounts also cross thread boundaries
   ($446.50 is a `DC1-26-5992` invoice assessed under `DC1-27-5628`), so this cannot be resolved by
   permuting serials within a thread, and the letters carry no invoice number and no treatment date
   to resolve it by.

Two supporting defects, both verified:

- **`_validate_settlement` reads `claim["petcover_reference"]` before `process_reply` learns it**
  (`claim_status.py:949` reads the row; `:876-878` writes it, later in the same loop). For claim #2
  the reference was NULL at check time, so the thread-prior query ran with `reference = None`,
  returned nothing, and the flag claimed a "fresh $150 excess this policy year" — one second after
  claim #8's letter in the same thread and policy year had stated `fixed_excess_stated: 150.00`.
- **The claimable subtotal is silently substituted with the invoice total in three places** —
  `_validate_settlement:923-925`, `visit_ledger:1243-1245`, `claim_detail:1644` — all spelling
  `invoice.get("claimable_amount")` then falling back to `invoice.get("amount")`. Claim #2's
  `invoice_data` has **no** `claimable_amount` key, so the $430.74 expectation in the dismissed flag
  was computed from $580.74, a number never submitted as a claimable subtotal. Three callers, one
  convention, no guard — the exact shape `app/openclaw/CLAUDE.md`'s collapse table exists to catch.

The stored events under-record what today's code can read: all five `approved` events lack
`age_contribution_stated`, which `extract_approval_amounts` captures from those same emails today
(re-run live 2026-08-04, 5/5). `_already_recorded` deliberately blocks a re-read from backfilling
it, so the gap is permanent in the log.

## What Changes

- **Split the one settlement check into two, because they ask different questions.** An *arithmetic*
  check re-adds the letter's own stated line items and confirms Petcover's sum; an *assessment*
  check compares what Petcover says was claimed against what we submitted. Only the second is a
  question for Petcover, and today they are fused into one flag whose wording blames the wrong one.
- **Stop guessing the excess when the letter states it.** `fixed_excess_stated` is on the letter and
  in the event detail; the "fresh $150 excess" inference is only for a claim with no letter figure.
- **Fix the read-before-learn ordering** so the thread-prior query sees the reference the same letter
  teaches.
- **One accessor for a claim's claimable subtotal**, which reports whether it is recorded rather
  than substituting the invoice total, plus a guard test that fails if a caller re-adds the
  fallback. **BREAKING** for display: claims whose `invoice_data` has no `claimable_amount` (live:
  #2, #16, #18, #19, #21) will read `Not recorded` where they previously showed the invoice total.
  **BREAKING** for behaviour, added 2026-08-04 after the guard test found two call sites this
  proposal had not: splitting a claim between pets (`claim_forms.apportion_between_pets`) apportioned
  the invoice total when no claimable subtotal was recorded — a share of a number that still carries
  the non-claimable lines, written into every resulting claim. It now refuses and names what is
  missing. Live effect: claim #16 (Echo, invoice on file, no recorded subtotal) can no longer be
  split until its invoice is re-matched.
- **Petcover's claims mail is excluded from the assistant's task capture**, added 2026-08-04 after the
  mailbox sweep. `processed_emails` is one dedupe gate shared by both pollers, so whichever ran first
  won permanently: five approval letters between 28/07 and 03/08 became tasks and produced no claim
  event at all (~$2.6k of settlements, the largest claimed at $2,521.46 / paid $1,638.95). Recovery is
  the existing `poll_petcover_status(reread=True)`, which is a live write and Justin's call — and
  belongs *after* this change ships, or the recovered letters get flagged by the very formula it
  replaces. See the `email-ingestion` delta.
- **Action cards carry the claim amount and the expected payment**, each with a named source, and
  print `Not recorded` when the source is absent.
- **Every action card says who is waiting on what.** `"confirm_resolved": "Petcover is waiting on
  you"` is applied to a review-the-numbers task and is misleading; the wording map gains an explicit
  waiting-party per kind.
- **An unexplained assessed-vs-submitted difference is not dismissible to invisible.** Dismissing it
  records the four letter figures and keeps the claim in the existing manual-review queue, rather
  than nulling the flag and leaving the append-only log as the only trace.
- **No historical rows are rewritten.** Re-routing a settlement moves money and is Justin's call
  after Petcover answers. This change adds no migration and no auto-correction.

Out of scope, deliberately: the dashboard's *estimate* (`_apply_excess_and_cap`) keeps its current
excess/cap math and gains no age-contribution term. See design.md — `dashboard-visit-ledger` carries
a prior decision against an "invented age-contribution percentage", and reading a percentage the
letter states for a settlement already received is a different act from projecting one onto an
unclaimed invoice. Conflating them would overturn a recorded decision as a side effect.

## Capabilities

### New Capabilities
- `claimable-subtotal-provenance`: one accessor answering "what is this claim's claimable subtotal,
  and is it recorded", the prohibition on substituting the invoice total, and the guard test that
  fails when a caller bypasses it.

### Modified Capabilities
- `settlement-validation`: the expected-payout formula becomes two checks — Petcover's own
  arithmetic re-added from the letter's stated figures, and assessed-versus-submitted — each with
  its own flag wording; the letter's stated fixed excess wins over the inferred one; the
  read-before-learn ordering is corrected; dismissal of an unexplained assessment difference stays
  visible.
- `telegram-bot`: action cards carry claim amount and expected payment with named sources and a
  `Not recorded` fallback, and every action kind states its waiting party.

## Impact

- `app/openclaw/claim_status.py` — `_validate_settlement`, `_APPROVAL_PATTERNS` (add
  `non_claimable_stated`, `age_contribution_percent`), `process_reply` ordering, `_ACTION_META`,
  `pending_actions`, `dismiss_mismatch`, `visit_ledger`, `claim_detail`.
- `app/openclaw/commands.py` — `_action_card_text`.
- `app/openclaw/claim_card.py` — `render_actions_summary` is a per-kind aggregate and needs no
  per-claim figures; unchanged unless the waiting-party wording surfaces there.
- `app/tests/test_core.py` — three new guard tests (design.md names them).
- No schema change, no `ALTER TABLE`, no write to the live DB.
- Docs: `openspec/BACKLOG.md` gains the unresolved assessed-vs-submitted questions; ADR-0011 and
  ADR-0020 are referenced, not amended (an ADR is Justin's call).
