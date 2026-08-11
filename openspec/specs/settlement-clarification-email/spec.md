# settlement-clarification-email Specification

## Purpose
Give Justin a way to close the loop on a Check-B assessment-difference or unrecorded-claimable-subtotal flag: gather flagged claims into one consolidated clarification email to Petcover, let him dismiss a settlement outright (Acceptable), or ask Petcover directly (More Info) — and, when their reply unambiguously confirms a claim's figure, auto-apply the same terminal dismissal without a second tap. See ADR-0030 for the one pre-existing named `send()` exception this change does NOT extend (draft-only by default; see design.md's Open Questions for the unresolved policy point).

## Requirements

### Requirement: An open settlement flag surfaces a review card with Acceptable / More Info actions
Every claim carrying an open, undismissed Check B assessment-difference flag or unrecorded-claimable-subtotal flag (see `settlement-validation`) SHALL surface a settlement-review card showing: the internal claim id, Petcover's reference and serial (when known), pet name, condition text (or that it isn't set), the amount we submitted, the amount Petcover stated it assessed (or that no subtotal was recorded), and either the invoice's line items (when `invoice_data.items` has fewer than 5 entries) or a link to the invoice PDF (5 or more entries, or no parsed items). The card SHALL offer exactly two actions, **Acceptable** and **More Info**.

This is one card type used at two points in a claim's timeline: before any clarification email exists (driven by the flag above), and again if a later Petcover reply doesn't resolve things (see the next requirement). Both uses render the same fields and offer the same two actions.

#### Scenario: Card renders line items
- **WHEN** claim #8's invoice has 3 line items
- **THEN** the review card lists each item's description and amount instead of linking the PDF

#### Scenario: Card renders a PDF link
- **WHEN** claim #14's invoice has 7 line items
- **THEN** the review card links the invoice PDF instead of listing items

#### Scenario: Condition not set
- **WHEN** a flagged claim has no `condition_text`
- **THEN** the card shows that the condition isn't set rather than omitting the field or guessing one

### Requirement: Acceptable is a terminal dismissal
Clicking **Acceptable** SHALL permanently dismiss the claim's open settlement flag, recording the dismissal using the same mechanism and figures as an existing manual dismiss (see `settlement-validation`). It SHALL NOT rewrite `claimable_subtotal`, any paid amount, or any other historical row. "Terminal" means this specific question is closed — it does NOT mean the claim can never be flagged again: a later, distinct letter carrying different figures raises a new, independent question exactly as today's one-way dismissal semantics already work.

#### Scenario: Justin accepts a settlement
- **WHEN** Justin clicks Acceptable on claim #8's review card
- **THEN** the flag is dismissed with the card's figures recorded, and no settlement row is rewritten

#### Scenario: A later, distinct letter still raises a new question
- **WHEN** a claim previously dismissed via Acceptable later receives a new settlement/approval event carrying different figures
- **THEN** that new event is validated independently and may flag again — the earlier Acceptable dismissal does not suppress it

### Requirement: More Info before a clarification email exists queues the claim into an open draft
Clicking **More Info** on a claim that is not yet in `awaiting_petcover_clarification` SHALL: ensure a single open clarification-request draft exists (creating one via `drafts().create` if none is open), append this claim's details to that draft's body, record a `clarification_requested` event on the claim, persist the draft's Gmail thread id against the claim, and move the claim into `awaiting_petcover_clarification` (see `claim-status-tracking`). This capability SHALL NOT call `send()` on the draft unless a future named exception (mirroring ADR-0030) explicitly permits it.

#### Scenario: First claim queued opens a new draft
- **WHEN** Justin clicks More Info on claim #8 and no clarification draft is currently open
- **THEN** a new Gmail draft is created containing claim #8's details, and claim #8 enters `awaiting_petcover_clarification`

#### Scenario: Second claim queued joins the same draft
- **WHEN** Justin clicks More Info on claim #14 while claim #8's draft is still open (not yet sent)
- **THEN** claim #14's details are appended to the same draft, and claim #14 also enters `awaiting_petcover_clarification` sharing that draft's thread id

### Requirement: A clarification reply is correlated by thread and auto-resolved only on an exact match
When a reply arrives on a clarification draft's Gmail thread (once Justin has sent it), the system SHALL correlate it to that batch by thread id, not by the general reference/Sr/pet-condition router used for unprompted Petcover mail. For each claim the batch covers, the system SHALL attempt to extract a `{claim identifier, confirmed amount}` pair from the reply. Where the confirmed amount matches the claim's recorded `claimable_subtotal` to the cent, the system SHALL apply the same terminal dismissal as clicking Acceptable, recording the reply's figures. The system SHALL NOT rewrite `claimable_subtotal`, any paid amount, or any other historical settlement row as part of this resolution.

Where no confirmed amount can be extracted for a claim, or the extracted amount does not match to the cent, or the reply addresses some but not all of the batch's claims, the unmatched claims SHALL remain in `awaiting_petcover_clarification` and their review card SHALL resurface, now also showing the reply's stated figure — this capability SHALL NOT guess a resolution from an approximate or ambiguous reply.

#### Scenario: Reply confirms the exact figure
- **WHEN** a reply on the batch's thread states claim #8's assessed amount as its recorded claimable subtotal to the cent
- **THEN** claim #8 is dismissed exactly as if Acceptable had been clicked, with the reply's figures recorded

#### Scenario: Reply gives a different or ambiguous figure
- **WHEN** a reply states an amount that does not match claim #8's recorded claimable subtotal, or states no extractable amount at all
- **THEN** claim #8 remains in `awaiting_petcover_clarification` and its review card resurfaces showing the reply's figure

#### Scenario: Reply answers only some of the batch
- **WHEN** a batch covers claims #8 and #14 and the reply only confirms claim #8's figure
- **THEN** claim #8 is dismissed and claim #14 remains open with its card resurfaced

### Requirement: More Info after an unresolved reply leaves a note, with no further automation
Clicking **More Info** on a claim whose review card resurfaced after an unresolved reply (i.e. it is already in `awaiting_petcover_clarification`) SHALL record a note on the claim's `flag` that Justin reviewed it and it remains unresolved. It SHALL NOT create or append to a new clarification draft, and SHALL NOT introduce a new event type — the claim simply stays visibly open for Justin to chase manually (e.g. by phone, or a follow-up he writes himself).

#### Scenario: Justin reviews an unresolved reply and still can't settle it
- **WHEN** Justin clicks More Info on claim #8's resurfaced card
- **THEN** the claim's flag records that it was reviewed and remains unresolved, no new draft or email is created, and claim #8 stays in `awaiting_petcover_clarification`

### Requirement: awaiting_petcover_clarification stays flagged until Acceptable, an auto-resolved reply, or explicit dismissal
`awaiting_petcover_clarification` SHALL be cleared only by an Acceptable click, an auto-resolved reply (same effect as Acceptable), or Justin's explicit dismissal — never implicitly by an unrelated event on the claim.

#### Scenario: Unrelated event arrives while awaiting clarification
- **WHEN** a claim in `awaiting_petcover_clarification` receives an unrelated event that isn't a reply on its clarification thread
- **THEN** it remains in `awaiting_petcover_clarification`

## Implementation notes (not requirements, kept here for the next reader)

- **Status literally stays `awaiting_petcover_clarification` after resolution.** Mirroring `confirm_resolved`'s treatment of `info_requested`/`suspended`, resolution (Acceptable or an auto-resolved reply) reuses `claim_status.dismiss_mismatch`, which clears `vet_claims.flag` and writes a `mismatch_dismissed` event but does **not** transition `status` again — `TRANSITIONS["awaiting_petcover_clarification"]` is `frozenset()`. Eligibility for the review card is a **flag** check (`_clarification_eligible`), not a status check, so a resolved claim naturally drops off `settlement_review_claims()` once its flag is cleared, without a second state.
- **`dismiss_mismatch` gained one optional parameter** (`reply_confirmation`) rather than a second writer: when given, it records the clarification reply's own stated figure instead of re-quoting the original approved/settled event's (which is the figure that raised the question, not the answer to it). `None` (the Acceptable button's path) keeps the pre-existing behaviour exactly.
- **Reply-to-claim attribution within a batch is by reference/serial substring match** (`_pair_identifies_claim`), not by amount alone: a batch can hold several claims, and a claim the reply never names must stay untouched rather than picking up a note about a figure meant for a sibling claim. The auto-resolve match itself is still amount-exact-only, per the requirement above — the identifier only scopes *which* claim a pair is about.
- **The "single open draft" is approximated by `sent_at IS NULL`**, since there is no `send()` call here to observe directly (this change does not extend ADR-0030). A claim newly flagged in the gap between an actual send and Petcover's first reply would still be appended to an already-sent draft — a known, accepted gap; add an explicit "mark sent" action if it bites in practice.
