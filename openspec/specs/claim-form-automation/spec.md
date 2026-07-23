# claim-form-automation Specification

<!-- NOTE: Partial spec. Base requirements for this capability are still pending sync from the active `vet-claim-automation` change; only the requirement modified by `petcover-claim-status-tracking` is captured here. -->

## Purpose
TBD — full purpose lands when the base `vet-claim-automation` change syncs.

## Requirements

### Requirement: Draft, never auto-send, the claim email
The system SHALL create a Gmail draft (using the `gmail.send`-scoped draft API) addressed to the insurer with the filled claim form attached, and SHALL NOT call Gmail's send endpoint on Justin's behalf. Once Justin sends the draft himself, the claim's status SHALL be advanced to `sent` so downstream status tracking (see `claim-status-tracking`) has a claim to attach Petcover's replies to.

#### Scenario: Claim drafted
- **WHEN** a claim reaches `drafted` status
- **THEN** a Gmail draft exists with the filled form attached, and the dashboard links to it for Justin to review and send himself

#### Scenario: Draft creation fails
- **WHEN** the Gmail draft-create call fails
- **THEN** the claim stays at `matched` and the failure is surfaced visibly, consistent with the existing Gemini-failure visibility requirement

#### Scenario: Justin sends the draft
- **WHEN** Justin marks a `drafted` claim as sent on the dashboard (v1: manual — no reliable automatic signal that a draft was sent)
- **THEN** every `drafted` claim sharing that claim's draft (a batch submission is one email) advances to `sent`, making the whole submission eligible to receive and be correlated with Petcover status-tracking events

### Requirement: The continuation box defaults to ticked
Every generated claim form SHALL have the "continuation of a previously claimed condition" box ticked (ADR-0012). Justin flips it during draft review for a genuinely new condition. (Successor behavior — derive from Condition Thread existence — is recorded in ADR-0012 and out of scope here.)

#### Scenario: Any claim form is generated
- **WHEN** a claim form is filled for a single claim or a batch
- **THEN** the continuation field is set to ticked
