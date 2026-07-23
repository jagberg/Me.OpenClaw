# claim-form-automation — delta

## ADDED Requirements

### Requirement: The continuation box defaults to ticked
Every generated claim form SHALL have the "continuation of a previously claimed condition" box ticked (ADR-0012). Justin flips it during draft review for a genuinely new condition. (Successor behavior — derive from Condition Thread existence — is recorded in ADR-0012 and out of scope here.)

#### Scenario: Any claim form is generated
- **WHEN** a claim form is filled for a single claim or a batch
- **THEN** the continuation field is set to ticked
