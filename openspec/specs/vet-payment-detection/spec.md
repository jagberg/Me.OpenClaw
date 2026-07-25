# vet-payment-detection Specification

## Purpose
Decide which bank charges are vet charges, cheaply — keywords first, LLM only for the ambiguous remainder — then get each one attributed to a pet, because the pet determines which insurer's process applies. `vet_detection.py`.

## Requirements

### Requirement: Classify transactions heuristic-first, LLM only for ambiguous cases
The system SHALL classify each new `bank_transactions` row as vet-related or not, using merchant-name keyword matching first, and SHALL call the LLM only for transactions that are ambiguous (pet/medical-adjacent with no keyword hit). A `non_vet_merchants` denylist SHALL suppress known false positives permanently.

**Provider note:** the original spec named Gemini specifically, because it was the only backend at the time. That is superseded by ADR-0009 — this goes through the `llm.extract()` seam and follows whatever `LLM_PROVIDER` is configured (Groq by default). The *shape* of the decision is unchanged and is the point: don't spend an LLM call where a keyword answers it. See `CLAUDE.md` — "don't add LLM calls where regex/keywords work".

#### Scenario: Obvious vet merchant
- **WHEN** a transaction's merchant name matches a known vet keyword
- **THEN** it is flagged vet-related with no LLM call

#### Scenario: Ambiguous merchant
- **WHEN** a transaction is pet/medical-adjacent but no keyword matches
- **THEN** the LLM is asked to judge, and the call is logged to `llm_calls` like any other

#### Scenario: Clearly unrelated merchant
- **WHEN** the merchant has no vet or medical signal at all
- **THEN** it is not flagged and no LLM call is made

#### Scenario: Known false positive
- **WHEN** a merchant matching a `non_vet_merchants` denylist entry is imported (e.g. a pet-shop-sounding grocer)
- **THEN** it never becomes a claim, without needing a fresh judgement each time

### Requirement: Every vet-flagged transaction must be attributed to a pet
A vet charge alone does not say which pet it is for, and the pets are on different insurers (Aari on Petcover, Echo on Bow Wow). The system SHALL require a pet before claim-form automation proceeds, and SHALL NOT guess one.

Attribution now happens three ways, not one: the dashboard picker, a Telegram tap (`pet_keyboard`), or automatically from patient facts printed on the matched invoice. The original spec named only the dashboard — the other two shipped later. Auto-assignment reads the vet's own document rather than inferring, so it does not violate the never-guess rule.

#### Scenario: Vet-flagged transaction awaiting attribution
- **WHEN** a transaction is flagged vet-related with no pet assigned
- **THEN** it is surfaced for attribution and does not proceed to claim-form filling until answered

#### Scenario: Pet named on the invoice
- **WHEN** the matched invoice prints a patient name matching a known pet (including the nickname table)
- **THEN** the pet is assigned from that document

#### Scenario: Attribution determines the insurer path
- **WHEN** a transaction is assigned to Aari
- **THEN** the Petcover claim-form-automation path applies

#### Scenario: Echo is still a dead end past matching
- **WHEN** a transaction is assigned to Echo
- **THEN** invoice matching proceeds normally, but claim-form automation stops and flags "Bow Wow Insurance claim process not yet defined" rather than guessing a process

## Open item — Echo / Bow Wow

The Echo path has been a dead end since this capability shipped, and remains one. Bow Wow's template format, submission method (email vs portal) and required fields are all unknown until Justin clarifies them with the insurer. Six claims (~$6.6k, of which two account for ~$5.4k) sit at `matched` behind it.

This is not a code gap and no button can clear it. Tracked in `openspec/BACKLOG.md`.
