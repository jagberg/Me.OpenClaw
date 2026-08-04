## ADDED Requirements

### Requirement: An action card states the claim amount and the expected payment, each from a named source

Every per-claim action card SHALL carry, in addition to the bank charge it already shows, two further
figures with a stated source:

- **Claim amount** — the claim's recorded claimable subtotal, obtained from the single accessor in
  `claimable-subtotal-provenance` (`invoice_data.claimable_amount`). This is what was actually
  submitted on the Petcover form (ADR-0007), and it is NOT the bank charge and NOT the invoice total.
- **Expected payment** — the visit-ledger row's `expected.value`, which
  `claim_status._apply_excess_and_cap` already computes and which `pending_actions()` already walks
  (it iterates `visit_ledger()`). No second calculation is introduced, so the card cannot disagree
  with the dashboard.

When either source is unavailable the card SHALL print `Not recorded` for that figure. It SHALL NOT
print `$0.00`, SHALL NOT omit the line silently, and SHALL NOT substitute the bank charge or the
invoice total. A recorded `claimable_amount` of `0.00` is a real value and prints as `$0.00` (live:
claim #20) — the distinction between a recorded zero and an absent key must survive to the card.

`expected` already carries `{available, value, note}` with notes such as `no invoice yet` and `no
policy excess/cap on file`; where a note exists the card SHALL show it beside `Not recorded` rather
than leaving the reason off the card. Echo has no `annual_excess`, `annual_cap` or
`policy_anniversary` on record as of 2026-08-04, so every Echo card takes this path.

Live case this exists for: claim #2 has no `claimable_amount` key, so its card must read
`Claim amount: Not recorded`, never the $580.74 invoice total that produced the wrong $430.74
expectation in the dismissed flag.

The summary card (`claim_card.render_actions_summary`) is a per-kind aggregate and carries no
per-claim figures; this requirement governs `commands._action_card_text` and any other per-claim card
builder.

#### Scenario: Both figures available
- **WHEN** an action card is built for a claim with `claimable_amount` $446.50 and a ledger `expected.value` of $296.50
- **THEN** the card shows the charge, `Claim amount: $446.50` and `Expected payment: $296.50`, all three distinct and labelled

#### Scenario: Claimable subtotal not recorded
- **WHEN** an action card is built for claim #2, whose `invoice_data` holds `amount` 580.74 and no `claimable_amount` key
- **THEN** the card shows `Claim amount: Not recorded` and the text `580.74` does not appear as the claim amount

#### Scenario: Expected payment unavailable
- **WHEN** the ledger row's `expected` has `available: false` with note `no policy excess/cap on file` (live: every Echo claim)
- **THEN** the card shows `Expected payment: Not recorded (no policy excess/cap on file)` and no numeric figure

#### Scenario: A recorded zero claimable subtotal
- **WHEN** an action card is built for claim #20, whose `claimable_amount` is 0.0
- **THEN** the card shows `Claim amount: $0.00`, distinct from `Not recorded`

### Requirement: Every action card names who is waiting on what

Each action kind's card text SHALL state the waiting party explicitly and correctly. The current
wording map (`claim_status._ACTION_META`) pairs a label with a blocks-phrase, and one pairing is
misleading in live use: `"confirm_resolved": ("Confirm resolved", "Petcover is waiting on you")` is
applied to `dismiss_mismatch`-adjacent review work where Petcover is not blocked on Justin at all.
A card asking Justin to check numbers SHALL NOT read as though Petcover is blocked on him.

Every kind SHALL declare its waiting party as one of: **Petcover waiting on you**, **you waiting on
Petcover**, **you waiting on the vet**, or **nobody waiting — for your review**. The declaration
SHALL be data on the kind, not a phrase assembled at the card site, so the Telegram card, the
dashboard and the chat agent cannot disagree — the same single-map reasoning as `status_labels.py`
(ADR-0021). A kind added without a waiting party SHALL fail a test rather than default to one:
naming the wrong waiting party is how a chase never happens, which
`app/openclaw/CLAUDE.md` already records for `info_requested`'s `owed_by`.

Specifically, `dismiss_mismatch` SHALL declare **nobody waiting — for your review** where the
difference is arithmetic, and **you waiting on Petcover** where it is an assessment difference (see
`settlement-validation`). Justin's report on this card was that it "said Petcover is waiting on me
but that doesn't seem to be the case if I have to just check the payment discrepancy".

#### Scenario: A settlement review card
- **WHEN** an action card is built for a claim carrying an arithmetic settlement difference
- **THEN** the card states that nobody is blocked and this is for Justin's review, and the phrase `Petcover is waiting on you` does not appear

#### Scenario: An assessment difference card
- **WHEN** an action card is built for a claim carrying an assessment difference
- **THEN** the card states that Justin is waiting on Petcover, names the submitted and assessed amounts, and asks him to check with Petcover

#### Scenario: A genuine Petcover-blocked card
- **WHEN** an action card is built for a claim where Petcover has requested information owed by Justin
- **THEN** the card states that Petcover is waiting on Justin

#### Scenario: A new action kind with no waiting party
- **WHEN** an action kind is added to `ACTION_PRIORITY` without declaring a waiting party
- **THEN** a test fails naming the kind, and no card is rendered with a defaulted waiting party
