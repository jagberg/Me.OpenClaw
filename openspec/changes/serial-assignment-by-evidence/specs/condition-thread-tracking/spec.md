## MODIFIED Requirements

### Requirement: A serial is assigned from evidence the letter carries, or not at all

When a Petcover letter cites a `(reference, Sr)` that no claim yet holds, the system SHALL choose the
claim from the amount the letter states: where **exactly one** claim awaiting a serial is worth that
amount — compared to the cent against its recorded claimable subtotal, falling back to the invoice
total only for a claim that never had a subtotal recorded — that claim receives the serial.

Where the stated amount matches **no** such claim, or matches more than one, the system SHALL NOT
assign the serial. It SHALL record the event unlinked with a `needs manual link` flag naming the
stated figure and the claims it considered, so the letter appears on the dashboard's review queue and
`link_event` can attach it in one action.

Where the letter states **no** amount at all (an acknowledgement), the system MAY fall back to the
oldest-transaction claim not yet serialized, and SHALL record on the event that the link was inferred
(`sr_assigned_by`), so the log distinguishes a guess from a citation.

**Why the ordering heuristic is no longer trusted alone.** It assigned a serial to "the
oldest-transaction claim not yet serialized" on the reasoning that Petcover's serials run oldest-first.
Petcover's status table of 2026-07-29 states a treatment date per serial, and against it the heuristic
was wrong on **all ten** serials held at the time. On 2026-08-05 it attached an under-excess refusal
for a $55.74 arthritis claim to a $2,521.46 ALT workup, moving a settled claim to `below_excess` and
leaving its own $1,638.95 settlement unlinked.

An unlinked event is visible and one action from correct; a confident wrong link is neither. This is
the same rule as "never guess a required claim field" (root `CLAUDE.md`) applied to routing.

#### Scenario: The stated amount identifies one claim
- **WHEN** a letter cites `DC1-27-5628` Sr 5, states `Total amount claimed: $446.50`, and exactly one claim awaiting a serial has a claimable subtotal of $446.50
- **THEN** that claim receives Sr 5, even when an older-transaction claim in the same submission is still unserialized

#### Scenario: The stated amount matches nothing we hold
- **WHEN** an under-excess letter cites `DC1-27-5628` Sr 4 and states `Amount claimed:$55.74`, and no claim awaiting a serial is worth $55.74
- **THEN** no claim receives Sr 4, no claim's status changes, and the event is recorded unlinked with a flag naming $55.74 and the candidate claim ids

#### Scenario: The stated amount is ambiguous
- **WHEN** two claims awaiting a serial are both worth the stated amount
- **THEN** neither is chosen and the event is left for a manual link

#### Scenario: An acknowledgement states no amount
- **WHEN** an acknowledgement cites a serial and carries no figures
- **THEN** the oldest-transaction unserialized claim receives it, and the event records `sr_assigned_by` as a heuristic

### Requirement: The under-excess refusal letter's figures are captured

The system SHALL extract the amount claimed and the fixed excess from Petcover's under-excess refusal
letter, which writes `Amount claimed:` and `Less Fixed excess:` — without the `Total` prefix the
approval template uses. Until 2026-08-05 that template yielded **no** figures at all, which is why the
letter that mis-assigned claim #12 had nothing to route by.

#### Scenario: An under-excess letter is read
- **WHEN** a letter states `Amount claimed:$55.74Less Fixed excess:$105.00Outstanding excess:$-49.26`
- **THEN** `claimed_amount` is 55.74 and `fixed_excess_stated` is 105.00
