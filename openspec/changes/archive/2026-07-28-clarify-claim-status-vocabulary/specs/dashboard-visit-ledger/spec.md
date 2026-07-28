## ADDED Requirements

### Requirement: Ledger status chips come from the shared vocabulary
The dashboard ledger and the `/basic` card view SHALL render a claim's state using the shared display vocabulary (`claim-status-vocabulary`), not a per-template label map. Neither template SHALL define its own status→wording table.

#### Scenario: A blocked claim reads as blocked
- **WHEN** the ledger renders a `matched` claim whose pet's insurer claim process is not defined
- **THEN** the chip states it is blocked on a missing claim process, and the same wording appears in `/basic`

#### Scenario: Chip wording changes in one place
- **WHEN** a label is renamed in the shared vocabulary
- **THEN** both the ledger chip and the `/basic` line change with no template edit

#### Scenario: Raw status still available on the claim detail
- **WHEN** Justin opens a claim's detail
- **THEN** the underlying stored status is still shown, so the derived label never hides what the pipeline recorded
