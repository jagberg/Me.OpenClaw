## MODIFIED Requirements

### Requirement: Settlements are validated against expected payout, bucketed by the claim's own transaction date

On a settlement or approval event carrying a paid amount, the system SHALL run **two independent
checks** and SHALL NOT fuse them into one flag, because they ask different questions of different
parties:

**Check A — Petcover's own arithmetic.** When the letter states its own line items, expected paid
SHALL be re-added from those stated figures and nothing else:

```
expected_paid = (claimed_amount - fixed_excess_stated - non_claimable_stated
                 - percentage_excess_stated)
                * (1 - age_contribution_percent)
```

with `age_contribution_percent` read from the letter (`Age Contribution: $12.25 [35%]` → `0.35`) and
each absent term treated as `0.00`. A difference greater than `SETTLEMENT_TOLERANCE` ($2.00) flags
the claim naming Petcover's own figures — this is an arithmetic dispute with Petcover and the only
check that may say so.

Verified against **all ten** live approval letters (2026-08-04), exact to the cent:

| reference | claimed | fixed excess | non-claimable | age contrib | paid |
|---|---|---|---|---|---|
| `DC1-27-5628` Tr 2 | $35.00 | $0.00 | $0.00 | $12.25 [35%] | $22.75 |
| `DC1-27-5628` Tr 5 | $446.50 | $49.26 | $0.00 | $139.03 [35%] | $258.21 |
| `DC1-27-5628` Tr 6 | $45.00 | $0.00 | $0.00 | $15.75 [35%] | $29.25 |
| `DC1-27-5628` Tr 7 | $132.50 | $0.00 | $0.00 | $46.38 [35%] | $86.13 |
| `DC1-27-5628` Tr 8 | $580.74 | $0.00 | **$135.00** | $156.01 [35%] | $289.73 |
| `DC1-26-5992` Tr 1 | $351.50 | $150.00 | $0.00 | $70.53 [35%] | $130.97 |
| `DC1-26-5992` Tr 2 | $35.00 | $0.00 | $0.00 | $12.25 [35%] | $22.75 |
| `DC1-26-5992` Tr 3 | $2,521.46 | $0.00 | $0.00 | $882.51 [35%] | $1,638.95 |
| `DC1-26-5992` Tr 4 | $135.00 | $0.00 | $0.00 | $47.25 [35%] | $87.75 |
| `DC1-26-5993` Tr 1 | $944.50 | $150.00 | $0.00 | $278.08 [35%] | $516.42 |

**Revision, 2026-08-04 (same day, before implementation).** The first five letters were the only ones
read when this delta was written; a full mailbox sweep found five more, and two of them change what
the requirement has to say:

- **`DC1-27-5628` Tr 8 is the only letter with a non-zero non-claimable amount**, and so the only one
  that distinguishes this formula from the post-mortem's `(claimed − excess) × 0.65`. That form gives
  $377.48 where Petcover paid $289.73 — wrong by $87.75. The formula above gives $289.73 exactly. The
  ordering it proves: excess and non-claimable come off *before* the age contribution, and the age
  contribution is charged on what remains ($445.74 × 0.35 = $156.01, as printed).
- **A fifth deduction line exists: `Percentage Excess: $0.00 [0%]`**, on all ten letters. It has never
  been non-zero, so its position in the order of operations is unverified; it is captured and deducted
  with the others, and a comment in `_check_petcovers_arithmetic` names that as the first thing to
  re-read if Check A ever fires on an otherwise sound letter.

Subtracting the four stated dollar deductions (`claimed − excess − non-claimable − age contribution −
percentage excess`) reaches the paid amount exactly on all ten and needs no percentage at all. The
multiplicative form above is kept because it verifies the rate the letter *states* rather than
trusting a dollar figure to be internally consistent, and $2.00 of tolerance absorbs the half-cent
rounding it introduces (`(944.50 − 150) × 0.65 = 516.425` against their $516.42).

**Check B — does the assessed amount match what we submitted under this serial.** `claimed_amount`
SHALL be compared to this claim's recorded claimable subtotal (see `claimable-subtotal-provenance` —
never the invoice total, and no comparison at all when the subtotal is unrecorded). A difference
greater than `SETTLEMENT_TOLERANCE` flags the claim as an **assessment difference**, naming: this
claim's id, its reference and serial, the amount we submitted, and the amount Petcover states it
assessed. The system SHALL NOT re-route the settlement and SHALL NOT rewrite any historical row —
re-routing a settlement moves money and is Justin's decision.

**The flag SHALL name the likelier cause, and it is usually ours.** Where another claim's invoice
matches Petcover's stated figure exactly, the flag SHALL say so and point at our serial→claim map
rather than telling Justin to ask Petcover; only where no invoice of ours matches is it a question for
them. Evidence, 2026-08-04: Petcover's status table of 2026-07-29 states a **treatment date per
serial**, and against it every serial we hold was on the wrong claim (0 for 10) while every letter's
stated amount matched its true claim's invoice to the cent (7 for 7). `_claim_for_sr` assigns a serial
to "the oldest-transaction claim not yet serialized" — an inference over Petcover's ordering that
nothing confirmed — so the disagreement this check surfaces is, on all live evidence, our own.
Correspondingly, an event whose serial was assigned by that heuristic SHALL record the fact
(`sr_assigned_by`), because the log could not previously distinguish a guessed link from a cited one.

This revises the original requirement's instruction to word the flag "as a question for Petcover".
That wording came from a conclusion — *"we cannot determine the correct mapping and should not try"* —
which the status table refutes: the mapping is determinable, from a document already in the mailbox.

**The letter's stated excess wins.** When `fixed_excess_stated` is present on the event, the system
SHALL use it and SHALL NOT emit the phrase "fresh $150 excess this policy year" or its
already-used counterpart. The inferred excess (below) applies only when no letter figure exists.

**Fallback when the letter states no line items** (the older PDF-attachment settlement style, which
carries only `Amount Claimed` and `Total Payable`): the system SHALL determine the claim's policy
year from **its own transaction date** (never from when the reply was processed) and compute expected
payout = the claim's recorded claimable subtotal minus the $150 excess (only when the thread has no
other approved/settled claim whose own transaction also falls in that same, current policy year),
bounded by the pet's remaining annual cap for that year, flagging a difference beyond tolerance in
**either direction**. Every flag is a warning to review, not an assertion that Petcover is wrong. The
system SHALL NOT auto-dispute and SHALL NOT send mail.

**Ordering.** Any query that reads the claim's `petcover_reference` to find thread siblings SHALL see
the reference the current letter teaches. Today `_validate_settlement` runs at
`claim_status.py:867` and reads the row's reference at `:949`, while `process_reply` writes the
learned reference at `:876-878` — later in the same loop iteration. So on the letter that teaches
the reference the sibling query runs with `reference = None` and finds nothing. Live consequence: on
2026-07-30 claim #2 was flagged "fresh $150 excess this policy year" one second after claim #8's
letter in the same thread (`DC1-26-5992`) and the same policy year had stated
`fixed_excess_stated: 150.00`.

#### Scenario: Petcover's arithmetic is correct and the assessed amount matches
- **WHEN** an approval letter states claimed $446.50, fixed excess $49.26, non-claimable $0.00, age contribution 35%, paid $258.21, and the claim's recorded claimable subtotal is $446.50
- **THEN** no flag is raised and the normal notification is sent

#### Scenario: Petcover's arithmetic is correct but they assessed a different amount
- **WHEN** an approval letter for claim #8 (`DC1-26-5992` Sr 1, recorded claimable subtotal $446.50) states claimed $351.50, fixed excess $150.00, age contribution 35%, paid $130.97
- **THEN** Check A passes — $(351.50 − 150.00) × 0.65 = $130.97 is within $2.00 of the stated paid amount — and no arithmetic flag is raised
- **AND** Check B flags an assessment difference naming claim #8, `DC1-26-5992` Sr 1, submitted $446.50, assessed $351.50
- **AND** the flag does not contain the words "fresh $150 excess this policy year", and no historical row is rewritten

#### Scenario: Petcover's arithmetic does not add up
- **WHEN** a letter states claimed $200.00, fixed excess $0.00, non-claimable $0.00, age contribution 35% and paid $150.00, where $(200.00 − 0.00) × 0.65 = $130.00
- **THEN** the claim is flagged as an arithmetic difference naming all four of Petcover's stated figures and the $20.00 gap

#### Scenario: The letter states a fixed excess, so none is inferred
- **WHEN** an approval letter states `fixed_excess_stated` of $150.00
- **THEN** the expectation uses $150.00 and no flag or notification contains the phrase "fresh $150 excess this policy year"

#### Scenario: The reference is learned by the same letter being validated
- **WHEN** an approval letter both teaches a claim its `petcover_reference` and states a paid amount, and a sibling claim already carries that reference with an approved event in the same policy year
- **THEN** the sibling is found — the validation sees the learned reference, not NULL — and the excess is not treated as fresh

#### Scenario: The claimable subtotal is not recorded
- **WHEN** an approval letter states a paid amount for a claim whose `invoice_data` has no `claimable_amount` key (live: claim #2)
- **THEN** Check B does not run and no expected-payout figure is produced from the invoice total
- **AND** the claim is flagged that its claimable subtotal is not recorded, naming the letter's stated figures

#### Scenario: Older settlement style with no line items
- **WHEN** a settlement email carries only `Amount Claimed` and `Total Payable` and no fixed-excess or age-contribution line
- **THEN** the transaction-date-bucketed excess/cap fallback runs and a difference beyond $2.00 in either direction is flagged

#### Scenario: Petcover pays more than expected
- **WHEN** the reported paid amount exceeds our expectation by more than the tolerance
- **THEN** the claim is still flagged — both checks are bidirectional, not one-way shortfall tests

## ADDED Requirements

### Requirement: The approval letter's full line-item breakdown is captured

The system SHALL extract and persist, from each `Claim Approval` letter, every figure the letter
states: `claimed_amount`, `paid_amount`, `fixed_excess_stated`, `non_claimable_stated`,
`age_contribution_stated`, `age_contribution_percent` and `percentage_excess_stated`. This is the
only email in the lifecycle that states them.

Two gaps as of 2026-08-04, both verified live: there is no pattern for `Non-claimable amount` at all,
and none for the bracketed percentage in `Age Contribution: $12.25 [35%]`. All five live `approved`
events also lack `age_contribution_stated` even though today's `extract_approval_amounts` captures it
from those same five emails when re-run (5/5, 2026-08-04) — the code was deployed after the events
were written. `_already_recorded` blocks a re-read from backfilling them, so the log's under-record
is permanent and SHALL be treated as a known historical gap, not repaired by re-reading mail.

#### Scenario: A full breakdown is captured
- **WHEN** a `Claim Approval` letter body contains `Total amount claimed: $446.50`, `Fixed excess $49.26`, `Non-claimable amount $0.00`, `Age Contribution: $139.03 [35%]`, `Paid by us: $258.21`
- **THEN** the recorded event detail carries `claimed_amount` 446.50, `fixed_excess_stated` 49.26, `non_claimable_stated` 0.00, `age_contribution_stated` 139.03, `age_contribution_percent` 0.35, `paid_amount` 258.21

#### Scenario: A term the letter omits
- **WHEN** a letter states no `Non-claimable amount` line
- **THEN** `non_claimable_stated` is absent from the detail and the arithmetic check treats it as $0.00, rather than the key being stored as a guessed zero

#### Scenario: An existing under-recorded event
- **WHEN** an `approved` event already stored without `age_contribution_stated` is encountered
- **THEN** the arithmetic check is skipped for that event rather than run against a missing term, and the gap is reported as historical

### Requirement: An unexplained assessment difference is not dismissible to invisible

Dismissing a settlement difference SHALL record, on the `mismatch_dismissed` event, the figures the
difference was made of — the letter's stated claimed and paid amounts, the claim's recorded claimable
subtotal, and which check raised it — not only the flag's prose. Where the dismissed difference was
an **assessment** difference (Check B), the claim SHALL remain listed in the existing dashboard
manual-review queue after dismissal, because the question is still open with Petcover.

Dismissal is one-way by design: `_validate_settlement` has one caller
(`claim_status.py:867`, inside `process_reply`) and `_already_recorded` blocks a re-read from
re-flagging, so nothing un-dismisses and only a new letter carrying a paid amount flags again. That
is correct for an arithmetic difference Justin has checked and wrong for a question he has asked
Petcover and not yet heard back on. Live proof it is wrong today: event 58 (2026-08-04) holds claim
#2's entire dismissed difference in `detail.dismissed_flag` as prose, and the claim's `flag` is NULL —
the append-only log is the only trace, and nothing on any surface says a question is outstanding.

No new table and no new dashboard surface: reuse the review queue that
`claim_status.dashboard_lists()` already builds.

#### Scenario: An arithmetic difference is dismissed
- **WHEN** Justin dismisses a difference raised by Check A
- **THEN** the `mismatch_dismissed` event records the stated claimed, paid, excess and non-claimable figures, the recorded claimable subtotal, and `check: "arithmetic"`, and the claim leaves the review queue

#### Scenario: An assessment difference is dismissed
- **WHEN** Justin dismisses a difference raised by Check B
- **THEN** the event records the same figures with `check: "assessment"`, and the claim remains in the dashboard manual-review queue with the submitted and assessed amounts visible

#### Scenario: The card offers the dismissal
- **WHEN** an action card is built for a claim carrying an assessment difference
- **THEN** the card states the submitted and assessed amounts and that the next step is asking Petcover, and does not imply the difference is Justin's to resolve alone
