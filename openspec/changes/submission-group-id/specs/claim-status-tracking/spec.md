## ADDED Requirements

### Requirement: A submission has a short derived group id

The system SHALL derive a human-readable identifier for a Submission from the claims composing it: `S` followed by their claim ids in ascending order joined by `+` (`S6+7`, `S18+19+21`). A claim with no Gmail draft is its own submission (`S12`).

The id SHALL be derived, never stored, and SHALL group claims by the same rule the notification and correlation paths already use (shared `draft_id`, else the claim alone) so a submission cannot be described two different ways in two places.

Rationale: a submission's only identity before Petcover replies is the opaque Gmail `draft_id`, which is unsayable. Building the id out of claim ids means it carries the values Justin actually acts on (`/mark 6 …`) instead of introducing a second vocabulary beside them. Once Petcover's claim reference is learned it remains the preferred display label — the group id fills the gap from `drafted` until then.

#### Scenario: Two claims sharing one draft
- **WHEN** claims #6 and #7 share one `draft_id`
- **THEN** both resolve to the group id `S6+7`, regardless of the order the claims were read in

#### Scenario: A claim with no draft
- **WHEN** a claim has no `draft_id`
- **THEN** its group id names only itself (`S12`) and it is never grouped with another claim

### Requirement: Submission-level outstanding work yields one entry per submission

The shared outstanding-work derivation (`claim_status.pending_actions()`) SHALL emit one entry per **submission** for action kinds that act on a whole submission, not one entry per claim. The entry SHALL carry the full member claim id list and the submission group id, and SHALL also carry a single representative claim id — the lowest member id — so that existing per-claim consumers (tap tokens, keyboards, the mutation itself) keep working unchanged.

The set of submission-level kinds SHALL be stated explicitly rather than inferred from the presence of a `draft_id`. Today it is `mark_sent` alone: every other kind fires at or before `matched`, where no draft and therefore no batch exists, or is driven by per-claim status events.

Rationale: sending one Gmail draft sends every claim in it, and `mark_sent` has always advanced the whole group at the data layer. Emitting one action per claim asked Justin to tap N times for one email, and made the second tap land on an already-sent claim. Verified live 2026-07-25: three batches (`#6+#7`, `#8+#22`, `#18+#19+#21`) each produced duplicate cards.

#### Scenario: A batched submission awaiting send
- **WHEN** claims #6 and #7 are both `drafted` and share one `draft_id`
- **THEN** the outstanding-work list contains exactly one `mark_sent` entry, carrying both claim ids, the group id `S6+7`, and representative claim id 6

#### Scenario: A per-claim action on a batched claim
- **WHEN** two claims sharing a draft each carry an unresolved info-request event
- **THEN** each still yields its own `confirm_resolved` entry — only submission-level kinds collapse

#### Scenario: Unbatched claims are unaffected
- **WHEN** a `drafted` claim has no `draft_id`
- **THEN** it yields one `mark_sent` entry naming only itself

#### Scenario: A collapsed entry's amount and date
- **WHEN** a submission's members were charged on different dates for different amounts
- **THEN** the entry's amount is the members' total (one email, one thing being confirmed) and its date is the **oldest** member's, because a visit stops being claimable at a year and a submission expires with its eldest member

#### Scenario: A date-filtered question about a batch
- **WHEN** outstanding work is requested for a date range and a submission's members straddle that range's edge
- **THEN** the submission is matched on the collapsed (oldest) date and reported as one item naming every member, rather than appearing once per in-range member

### Requirement: An answer about a submission names every claim in it

Any surface reporting a submission-level action SHALL name every member claim id, not just the representative used for tap tokens. Naming only the representative would silently drop the other claims from an answer Justin acts on.

Rationale: found in review of this change — the collapse gave the chat agent's outstanding-work answer a single `claim_id` to render, which would have printed `#6` and omitted `#7` from the very batch it was describing. The standing "every claim reference carries its claim id" requirement is satisfied only if *every* referenced claim's id appears.

#### Scenario: Chat answer about a batched submission
- **WHEN** the agent reports outstanding work including a two-claim submission
- **THEN** the line carries the group id and both claim ids

### Requirement: Marking an already-sent submission reports a no-op, not a rejection

When mark-sent is invoked for a claim already at `sent` that belongs to a submission already marked sent, the system SHALL report that the submission was already marked sent and name its group id, rather than reporting that the claim "isn't drafted". No data SHALL change and the result SHALL still indicate that nothing was done.

This SHALL apply to `sent` only: a claim that has advanced past sent through Petcover's replies (`acknowledged`, `approved`, `settled`, …) MUST NOT be described as "already marked sent", because that would misstate where it actually is.

Rationale: the first tap on a batch advances every member, so any second tap — from a sibling card, an older push, or the dashboard — necessarily lands on an already-sent claim. Justin read `Claim #7 isn't drafted (status: sent)` as a failure when the send had in fact succeeded. Old messages stay tappable indefinitely, so this response is permanent, not transitional.

#### Scenario: Tapping the sibling of an already-sent batch
- **WHEN** mark-sent is invoked for claim #7 after claim #6's tap advanced the whole `S6+7` submission
- **THEN** the response says submission `S6+7` was already marked sent, no claim changes, and the result indicates nothing was done

#### Scenario: A claim that moved past sent
- **WHEN** mark-sent is invoked for a claim now at `acknowledged`
- **THEN** the response names its actual status and does not claim it was already marked sent
