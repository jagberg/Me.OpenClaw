# ADR-0007: Bank charge as claim ceiling; claim the claimable subtotal, not the charge

**Date**: 2026-07-18
**Status**: accepted
**Deciders**: Justin

## Context

Invoice matching originally required the invoice total to approximately equal the bank charge (3% tolerance). Real data broke this twice: a card surcharge made the charge exceed the invoice ($580.74 invoice charged as $585.39), and one charge covered two invoices for different pets ($177.50 = $35 + $142.50). Separately, invoices can mix claimable treatment with routine care (vaccination, worming) that insurers exclude.

## Decision

The bank charge is the ceiling on what can be claimed, not an equality target: an invoice matches when its total ≤ the charge (+1c float tolerance). Extraction returns per-line-item amounts; the claim form carries the claimable subtotal (line items minus the `NON_CLAIMABLE_KEYWORDS` routine-care list), never the bank amount. A gap beyond a plausible surcharge (>2%) flags "possible additional invoice" instead of blocking the match.

## Alternatives Considered

### Alternative 1: Keep ≈-equality with a bigger tolerance
- **Pros**: One-line change.
- **Cons**: No tolerance covers the multi-invoice case ($35 vs $177.50 is an 80% gap); widening it invites false positives without modeling why amounts differ.
- **Why not**: The differences aren't noise — they're structure (surcharge, bundled invoices) the rule should express.

### Alternative 2: Let Gemini judge claimability per line item
- **Pros**: Handles wording variations.
- **Cons**: Burns the 20/day free-tier quota; non-deterministic on a money path.
- **Why not**: A keyword list Justin curates is deterministic, testable, and free; Gemini already provides the itemization.

## Consequences

### Positive
- Both real failure cases match correctly; routine-care-only invoices are flagged, never drafted.
- Claim amounts are defensible: exactly what the invoice supports.

### Negative
- Ceiling-only matching is more permissive — a wrong small invoice could match a large charge if merchant + date window also align (accepted; ambiguity remains rare with those constraints).

### Risks
- Keyword list incompleteness → a routine item slips into a claim; insurer rejects that line, no financial harm. Justin extends the list as cases appear.
