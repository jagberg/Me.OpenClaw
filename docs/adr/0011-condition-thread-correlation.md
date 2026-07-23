# ADR-0011: Petcover correlation is per Condition Thread, not per Submission

**Date**: 2026-07-23
**Status**: accepted (design agreed; implementation pending)
**Deciders**: Justin

## Context

The system assumed one Submission (one draft, ≤4 invoices) earns one Petcover reference, and `find_claims_by_reference` applies every incoming event to all claims sharing that reference. Mining the full 2022–2026 Petcover email history disproved the model:

- **References are reused for years.** DC1-27-5628 (Aari, Arthritis): SR1 info-requested Jan 2026, suspended, settled 3 Feb 2026 — then the July 2026 submission arrived as Sr 2/3/4 under the *same* reference. ELD-24-2146 (Loki) ran three ack→settle cycles over 8 months on one reference.
- **A reference is a (pet, condition) pairing.** Each pet holds several references, one per condition (Aari: arthritis, facial growth, pancreatitis, lick granulomas — four refs).
- **Petcover assigns the condition themselves.** A July submission whose claims all carried `condition_text = Arthritis` produced an ack in a *new* Lick Granulomas thread — their clinicians file documents by what the invoice shows, not by what our form says.
- **Events target reference + Sr.** Suspension/request letters cite "DC1-27-5628 SR1"; Sr is the per-document counter inside the thread.
- Acknowledgement letters contain pet name (misspelled "Ari" — nickname table handles it), condition text, reference, and Sr — but **no invoice amounts or dates**.

Without a fix, the next arthritis submission stamps events onto the already-settled claims 18/19/21, and any letter citing an Sr cannot be routed to the claim it is actually about.

## Decision

1. **Condition Thread becomes an entity** (see `CONTEXT.md`): (pet, reference), living as long as the condition. Claims join a thread at acknowledgement; `petcover_sr` is stored per claim.
2. **Event routing**: a letter citing reference + Sr targets that one claim. A reference-only letter targets the thread's **non-terminal** claims only (not settled, not declined) — reuse of an old reference can never disturb finished claims.
3. **Ack→claim mapping** (learning reference + Sr for a claim, per Justin's rule): match the ack's printed condition against the submission's condition text first; if that doesn't decide it, assume the ack belongs to the **most recently sent** un-referenced submission for that pet; multiple same-day acks map last-ack→last-sent, working backwards (send order mirrors ack order).
4. **Thread isolation**: a declined thread is terminal for its own claims only. Other threads — including other threads fed by the same Submission — proceed unaffected.

## Alternatives considered

- **Keep per-submission references** — empirically false; would keep producing `unclassified` events and cross-thread contamination.
- **Date-windowed correlation** — rejected long ago (ADR-0008 context): a claim's transaction can be a year older than its submission; now doubly wrong since threads span years by design.
- **Ask Justin per ambiguous ack (Telegram button)** — kept as the last-resort fallback when content + recency both fail, consistent with the never-guess rule, but the history shows condition + recency resolves the normal cases.

## Consequences

### Positive
- Reference reuse (proven behavior) routes correctly; settled claims stay settled.
- Per-Sr routing makes info-request/suspension letters actionable against the exact claim.
- The excess model ($150 per condition per year) now has a home: the thread is the natural place to track excess consumption and expected payout.

### Negative / Risks
- Schema change on the live DB (`petcover_sr`, thread bookkeeping) — manual DDL per the live-schema rule.
- Petcover may re-condition a document (our condition_text is input only); the mapping rule tolerates this by preferring their printed condition over ours.
- The LIFO same-day assumption is a heuristic; if it ever misroutes, the correction path is the existing unmatch/manual tooling.
