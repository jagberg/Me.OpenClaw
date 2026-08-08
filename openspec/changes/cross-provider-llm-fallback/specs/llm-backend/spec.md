## ADDED Requirements

### Requirement: The fallback chain crosses providers when one provider is exhausted

The model fallback chain SHALL be able to continue into a **different provider** once every configured model of the current provider has exhausted its daily budget. A chain whose links are all one provider SHALL NOT be considered adequate insurance, because a provider-wide exhaustion defeats every link at once.

This extends ADR-0017 rather than replacing it. Per-model fallback within a provider stays the first resort and stays correct; the cross-provider hop is the floor beneath it.

#### Scenario: Every model of the primary provider is exhausted

- **WHEN** the primary provider's model and all of its configured same-provider fallbacks report a per-day budget exhaustion
- **THEN** the next candidate SHALL be a model belonging to a different configured provider
- **AND** the call SHALL succeed if that provider answers

#### Scenario: A per-minute limit, not a per-day one

- **WHEN** the failure is a per-minute rate limit rather than a daily budget exhaustion
- **THEN** the chain SHALL NOT advance to another provider, because waiting is the cure and switching wastes a budget that is not spent

#### Scenario: No provider can serve the call

- **WHEN** every model of every configured provider is exhausted
- **THEN** the call SHALL raise `LLMUnavailableError` naming the exhaustion
- **AND** SHALL NOT return an empty or partial result that reads like an answer

### Requirement: More than one provider's client can exist in one process

The client cache SHALL be keyed by provider rather than being a single module-level client. A single cached client pins the whole process to one provider and is the reason a second provider in configuration currently buys nothing.

#### Scenario: A chain uses two providers in one call

- **WHEN** a single call falls through from one provider to another
- **THEN** each provider SHALL be reached through its own client, with its own base URL and key
- **AND** neither client SHALL be discarded or rebuilt for the other's sake

### Requirement: The model that answered is reported accurately across providers

The identity of the model that actually served a call SHALL be carried back through the call path rather than read from module-level state. It SHALL name the provider as well as the model once a chain can cross providers, because "which model answered" stops being unambiguous when two providers offer similarly-named models.

Disclosure of a downgrade is required by ADR-0017 and SHALL NOT become quieter here: a cross-provider downgrade is a larger change in behaviour than a cross-model one.

#### Scenario: A cross-provider fallback served the call

- **WHEN** a call is served by a provider other than the configured primary
- **THEN** the reported model SHALL identify that provider and model
- **AND** the downgrade SHALL be disclosed to the user in the same way a same-provider downgrade is

#### Scenario: Two calls run against different providers

- **WHEN** one call falls through to a second provider and a later call is served by the primary
- **THEN** each SHALL report the model that served it, with no leakage of the other's value

### Requirement: Daily-budget exhaustion is recognised per provider

A per-day exhaustion SHALL be recognised from each configured provider's own 429 body. A classifier written for one provider's wording produces, for any other provider, a chain that never advances and never says why.

#### Scenario: A provider reports exhaustion in its own wording

- **WHEN** a configured provider returns a 429 whose body indicates a per-day cap in that provider's own format
- **THEN** it SHALL be classified as a daily-budget exhaustion and the chain SHALL advance

#### Scenario: An unrecognised 429 body

- **WHEN** a 429 arrives whose body matches no known per-day pattern
- **THEN** it SHALL be treated as a per-minute limit (the safer default: wait rather than burn another provider's budget)
- **AND** the unrecognised body SHALL be logged so the gap is visible rather than silent
