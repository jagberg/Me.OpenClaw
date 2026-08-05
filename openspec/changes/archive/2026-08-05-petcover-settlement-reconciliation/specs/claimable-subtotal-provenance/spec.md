## ADDED Requirements

### Requirement: One accessor answers what a claim's claimable subtotal is, and whether it is recorded

The system SHALL expose exactly one function that resolves a claim's claimable subtotal from its
`invoice_data`, returning both the value and whether it was recorded. It SHALL return "not recorded"
when the `claimable_amount` key is absent, and SHALL NOT substitute `invoice_data.amount` (the
invoice total) in its place. `claimable_amount` is the line-item subtotal minus
`NON_CLAIMABLE_KEYWORDS` and, for a per-pet split, this claim's own share (ADR-0007, ADR-0019); the
invoice total is a different quantity and is never a stand-in for it.

A recorded `claimable_amount` of `0.0` SHALL be reported as recorded with value `0.0`, not as
missing — live claim #20 holds exactly that (invoice total $152.50, claimable $0.00) and it is a
real answer, not an absence.

Five live claims have no `claimable_amount` key as of 2026-08-04 (#2, #16, #18, #19, #21), so this is
the common case and not an edge case.

#### Scenario: The key is absent
- **WHEN** a claim's `invoice_data` contains `amount` but no `claimable_amount` key (live: claim #2, `amount` = 580.74)
- **THEN** the accessor reports the subtotal as not recorded and returns no value, and does NOT return 580.74

#### Scenario: The key is present
- **WHEN** a claim's `invoice_data` contains `claimable_amount` = 446.50
- **THEN** the accessor reports it as recorded with value 446.50

#### Scenario: A recorded zero
- **WHEN** a claim's `invoice_data` contains `claimable_amount` = 0.0 (live: claim #20)
- **THEN** the accessor reports it as recorded with value 0.0, and no consumer treats it as missing

#### Scenario: No invoice at all
- **WHEN** a claim has empty or null `invoice_data` (live: claims #4, #5, #17)
- **THEN** the accessor reports the subtotal as not recorded and returns no value

### Requirement: No consumer substitutes the invoice total for an unrecorded claimable subtotal

Every consumer of a claim's claimable subtotal SHALL obtain it from the single accessor above and
SHALL render or propagate "not recorded" rather than a substituted, zeroed or inferred figure. This
covers the **five** sites that carried the substitution on 2026-08-04. Three were known when this
delta was written — `claim_status._validate_settlement`, `claim_status.visit_ledger` and
`claim_status.claim_detail` — and two more were found by the guard test itself, which is the argument
for the guard in one line:

- `claim_status.dashboard_lists` — `invoice.get("claimable_amount") or invoice.get("amount") or
  detail.get("claimed_amount")`, in the settled-reconciliation row. Falling through to Petcover's own
  stated figure turns a difference into an agreement: the row exists to compare those two numbers.
- `claim_forms.apportion_between_pets` — split the *invoice total* between pets when no claimable
  subtotal was recorded, writing a share of a number that still carries the non-claimable lines into
  every resulting claim. This one changes behaviour rather than only wording, so it is called out in
  the proposal: a split of a claim with an invoice but no recorded claimable subtotal now **refuses**,
  naming what is missing, instead of silently apportioning the wrong base.

A settlement expectation SHALL NOT be computed at all when the claimable subtotal is not recorded;
the claim is flagged as missing the subtotal instead. This is the rule the dismissed flag on claim #2
broke: expected $430.74 was `580.74 − 150.00`, and $580.74 was the invoice total, never a submitted
claimable subtotal.

#### Scenario: A settlement arrives for a claim with no recorded claimable subtotal
- **WHEN** an approval letter states a paid amount for a claim whose `invoice_data` has no `claimable_amount` key
- **THEN** no expected-payout figure is computed or displayed, and the claim is flagged that its claimable subtotal is not recorded
- **AND** the flag names the claim id and the letter's stated figures, so the letter is not lost

#### Scenario: The ledger renders a claim with no recorded claimable subtotal
- **WHEN** the visit ledger renders such a claim
- **THEN** the claimable column reads "Not recorded" and the expected-reimbursement column is marked unavailable, and neither shows the invoice total

#### Scenario: The chat agent is asked about such a claim
- **WHEN** `claim_detail` is built for such a claim
- **THEN** its `claimable_amount` field is absent or explicitly null with a "not recorded" marker, and is not populated from `invoice_amount`

### Requirement: A mechanical guard fails when a caller re-adds the fallback

The test suite SHALL contain a test that scans `app/openclaw/*.py` and FAILS when any module other
than the single accessor reads `claimable_amount` with a fallback to `amount` — the shape
`invoice.get("claimable_amount", invoice.get("amount"))` and the two-statement `if claimable is None:
claimable = invoice.get("amount")` equivalent. The test SHALL assert its own detection by running its
matcher against a known-violating string, so a broken matcher fails loudly instead of passing
vacuously — the same self-check
`test_no_module_outside_claim_status_writes_the_status_column` carries.

Named: `test_no_module_substitutes_the_invoice_total_for_a_claimable_subtotal`.

This is the guard, and its absence is what let one convention rot across three call sites. Per
`app/openclaw/CLAUDE.md`'s collapse table, a declared rule with no mechanical guard is what every
incident in that table was before it became an incident.

#### Scenario: A fourth caller adds the fallback
- **WHEN** a module outside the accessor is changed to read `invoice.get("claimable_amount", invoice.get("amount"))`
- **THEN** `test_no_module_substitutes_the_invoice_total_for_a_claimable_subtotal` fails, naming the file and line

#### Scenario: The matcher itself breaks
- **WHEN** the test's own pattern is changed so it no longer matches the violating shape
- **THEN** the test's self-check fails, rather than the test passing with nothing detected
