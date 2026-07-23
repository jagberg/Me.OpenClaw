# ADR-0012: The claim form's continuation box defaults to ticked

**Date**: 2026-07-23
**Status**: accepted
**Deciders**: Justin

## Context

The Petcover form asks whether the claim continues a previously claimed condition. In code it was a per-call parameter (`_shared_fields(pet, continuation)`) that no caller meaningfully supplied — a human judgment with no owner. With Condition Threads (ADR-0011) the value is in principle derivable: continuation ⇔ a thread already exists for the (pet, condition).

## Decision

Default `continuation` to **true** on every generated form, unconditionally, for now. Justin reviews every draft before sending and can flip the box for a genuinely new condition. Thread-derived continuation (tick iff a matching Condition Thread exists) is the agreed successor once thread bookkeeping is implemented — this ADR records the interim default and the intended replacement.

## Alternatives considered

- **Derive from Condition Threads now** — the right end state, but thread storage doesn't exist yet; deferred rather than rejected.
- **Ask per claim on Telegram** — a third tap per claim (after condition and pet) for something history usually answers; tap fatigue for marginal accuracy.
- **Default false** — wrong more often: these pets' claims are dominated by ongoing conditions (arthritis, hepatitis-style threads spanning years), so "continuation" is the majority case.

## Consequences

- First-ever conditions will carry a wrongly ticked box unless Justin flips it during draft review — accepted; Petcover assigns the condition thread themselves regardless (ADR-0011), so the practical cost of a wrong tick is low.
- When thread derivation lands, this default becomes the fallback for claims whose condition matches nothing on record.
