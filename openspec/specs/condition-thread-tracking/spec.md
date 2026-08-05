# condition-thread-tracking Specification

## Purpose
Model a Petcover claim reference as a **Condition Thread** — one (pet, condition) pairing whose reference is reused for the life of the condition — and route Petcover's reply events to the correct claim within a thread using the reference and Petcover's per-document serial, never disturbing terminal (settled/declined) claims and never letting one declined thread block another.

See ADR-0011 for the email-mining evidence (reference reuse, per-Sr routing, ack correlation rules) behind these requirements.
## Requirements
### Requirement: A claim belongs to a Condition Thread with a per-document serial
A Petcover reference identifies a Condition Thread — one (pet, condition) pairing reused for the life of the condition (proven: settled and reused months apart). The system SHALL store, per claim, the thread's reference (`petcover_reference`) and Petcover's document serial (`petcover_sr`) learned from their letters, in either of two confirmed formats: adjacent to the reference ("DC1-27-5628 Sr 3") or its own distinctly-labeled field ("Treatment number: 3" — no reference adjacency, discovered live 2026-07-24 after being missed initially and briefly mis-routing an event to a whole thread instead of one claim, see ADR-0011's amendment).

#### Scenario: Acknowledgement carries reference and serial
- **WHEN** an acknowledgement correlates to a claim and contains reference `DC1-27-5628` and `Sr 3`
- **THEN** the claim stores reference `DC1-27-5628` and `petcover_sr = 3`

#### Scenario: Reference reused months after settlement
- **WHEN** a new acknowledgement arrives carrying a reference that only settled claims currently hold
- **THEN** the new claim joins the thread with its own Sr, and no settled claim's status or events are touched

### Requirement: Events route by reference and serial, never to terminal claims
The system SHALL route a classified Petcover event: (1) to the single claim matching (reference, Sr) when the letter cites a serial; (2) when the letter cites only a reference, to that thread's non-terminal claims only — claims whose status is `settled` or `declined` SHALL never receive routed events.

#### Scenario: Letter cites reference and serial
- **WHEN** a suspension letter cites "DC1-27-5628 SR1" and a claim holds that (reference, sr)
- **THEN** the event attaches to that claim alone

#### Scenario: Reference-only letter with settled siblings
- **WHEN** an info-request cites only `DC1-27-5628`, and the thread holds two settled claims and three acknowledged claims
- **THEN** the event attaches to the three acknowledged claims only

### Requirement: A declined thread never blocks other threads
Decline events SHALL be terminal only for the claims of their own thread. Claims in other threads — including threads fed by the same Submission — SHALL proceed unaffected.

#### Scenario: One of a submission's two threads is declined
- **WHEN** a submission's invoices were filed by Petcover into two threads and one thread receives a decline
- **THEN** only that thread's claims become `declined`; the other thread's claims keep their status and continue receiving events

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

