## ADDED Requirements

### Requirement: Classify stored transactions as vet-related via heuristic first, LLM only for ambiguous cases
The system SHALL classify each new `bank_transactions` row as vet-related or not, using merchant-name/category keyword matching first, and SHALL only call Gemini for transactions that are ambiguous (medical/pet-adjacent category with no keyword hit).

#### Scenario: Obvious vet merchant
- **WHEN** a transaction's merchant name matches a known vet keyword or the user's vet allowlist
- **THEN** it is flagged vet-related without any Gemini call

#### Scenario: Ambiguous merchant
- **WHEN** a transaction's category is medical/pet-adjacent but no keyword matches
- **THEN** Gemini is called to judge vet-relatedness, and the call is logged like other extraction calls

#### Scenario: Clearly unrelated merchant
- **WHEN** a transaction's merchant/category has no vet or medical signal at all
- **THEN** it is not flagged and no Gemini call is made

### Requirement: Ask which pet a vet-flagged transaction belongs to
Justin has two dogs on two different insurers (Aari on Petcover, Echo on Bow Wow Insurance) — a vet transaction alone doesn't say which pet it's for. The system SHALL prompt Justin (via the dashboard) to attribute each vet-flagged transaction to a specific pet before it can proceed to claim-form automation, since the pet determines which insurer's process and template apply.

#### Scenario: Vet-flagged transaction awaiting pet attribution
- **WHEN** a transaction is flagged vet-related and has no pet assigned yet
- **THEN** it's surfaced on the dashboard with a pet picker (Aari / Echo), and does not proceed to claim-form filling until answered

#### Scenario: Pet attribution determines the downstream insurer path
- **WHEN** Justin assigns a transaction to Aari
- **THEN** the Petcover claim-form-automation path applies (known and spec'd)

#### Scenario: Pet attribution to Echo is currently a dead end past matching
- **WHEN** Justin assigns a transaction to Echo
- **THEN** invoice matching still proceeds normally, but claim-form automation stops and flags "Bow Wow Insurance claim process not yet defined" instead of guessing a process — blocked until Justin clarifies it with them
