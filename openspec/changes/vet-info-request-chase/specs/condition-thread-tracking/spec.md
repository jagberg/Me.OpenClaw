## MODIFIED Requirements

### Requirement: A claim belongs to a Condition Thread with a per-document serial
A Petcover reference identifies a Condition Thread — one (pet, condition) pairing reused for the life of the condition (proven: settled and reused months apart). The system SHALL store, per claim, the thread's reference (`petcover_reference`) and Petcover's document serial (`petcover_sr`) learned from their letters, in any of four confirmed formats: adjacent to the reference with whitespace ("DC1-27-5628 Sr 3"), adjacent with a dot ("DC1-27-5628 Sr.8", "DC1-26-5992 sr.1"), or as its own distinctly-labeled field — "Treatment number: 3" (no reference adjacency, discovered live 2026-07-24 after being missed initially and briefly mis-routing an event to a whole thread instead of one claim, see ADR-0011's amendment) or "Serial Number: 2".

All text SHALL be normalized before reference and Sr extraction by mapping Unicode hyphens and dashes (U+2010–U+2015, U+2212) to ASCII `-`. Petcover's letters render the reference as `DC1‐26‐5992` with U+2010 non-breaking hyphens, which terminates an `[A-Za-z0-9-]+` capture after three characters. Confirmed live 2026-07-27: the letter about claim #8 (`DC1-26-5992 Sr 1`, Kings Vet) learned the reference `DC1`, failed its exact `(reference, Sr)` lookup, fell through to recency correlation, and attached to claim #2 (The Shire Vet) — a different pet visit entirely. Normalization happens once at the extraction seam so stored references stay canonical ASCII and cannot fail to match a previously stored one.

The reference SHALL be extracted by its context phrase where one is present ("Claim Reference:", "Claim Number", "Petcover Claim"), and otherwise by its own shape — `[A-Z]{2,4}-\d{2}-\d{4}` or `GABR-\d{4}`, matched case-insensitively — rejecting any candidate that sits inside the policy number (`GABR-0306-DC1-00000001R`), which is the reason bare patterns were originally excluded. The shape fallback exists because the vet-addressed cover note carries its reference only in a free-form subject (`Petcover claim for Ari DC1-27-5628 Sr.8`), where the context phrases match nothing and are additionally case-sensitive today. Petcover has used at least five subject shapes in two years; the reference's shape has been stable across all of them.

#### Scenario: Acknowledgement carries reference and serial
- **WHEN** an acknowledgement correlates to a claim and contains reference `DC1-27-5628` and `Sr 3`
- **THEN** the claim stores reference `DC1-27-5628` and `petcover_sr = 3`

#### Scenario: Reference written with non-breaking hyphens
- **WHEN** a letter renders the reference as `DC1‐26‐5992 Sr 1` using U+2010 hyphens
- **THEN** the reference `DC1-26-5992` and `Sr 1` are extracted, and the event routes to the claim holding that exact pair

#### Scenario: Serial written with a dot
- **WHEN** a letter cites `DC1-27-5628 Sr.8` or `DC1-26-5992 sr.1`
- **THEN** the serial is extracted as 8 and 1 respectively

#### Scenario: Serial as a labeled field
- **WHEN** a letter cites `Serial Number: 2` with no serial adjacent to the reference
- **THEN** the serial is extracted as 2

#### Scenario: Reference only in a free-form subject
- **WHEN** the only reference text is the subject `Petcover claim for Ari DC1-27-5628 Sr.8`, with no context phrase preceding it
- **THEN** `DC1-27-5628` is extracted by shape, case-insensitively

#### Scenario: Policy number must not be read as a reference
- **WHEN** text contains the policy number `GABR-0306-DC1-00000001R` and no claim reference
- **THEN** no reference is extracted, rather than capturing `GABR-0306` or `DC1-00000001R` from inside it

#### Scenario: Reference reused months after settlement
- **WHEN** a new acknowledgement arrives carrying a reference that only settled claims currently hold
- **THEN** the new claim joins the thread with its own Sr, and no settled claim's status or events are touched
