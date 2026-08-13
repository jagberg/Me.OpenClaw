## Context

`claim-status-tracking`'s existing rule: a claim's `info_requested`/`suspended` "needs your action" status survives every later event, including `settled`, until Justin explicitly taps confirm. Written for a real, documented pattern — Petcover's own follow-through is inconsistent, and a claim was observed getting repeated "request for X" emails before actual resolution, so `settled` alone was never trustworthy as "done."

Claim #13 (live, this session) is the case that rule wasn't written for: `settled`, `claimed_amount` $944.50 matching our recorded claimable subtotal exactly, `paid_amount` $516.42 reconciling fully against the stated excess and age contribution — Check A and Check B both pass, no flag. It still carries an unconfirmed `info_requested` (`owed_by: justin`) from weeks earlier. Justin's read: the settlement itself is proof whatever was needed arrived; continuing to ask him to confirm this is exactly the noise the app is supposed to cut through, not add to.

## Goals / Non-Goals

**Goals:**
- A claim that reaches `settled` AND validates clean (no Check A/B mismatch) auto-confirms any outstanding `info_requested`/`suspended` event, via the existing `confirm_resolved` — same action, new trigger.
- Leave the original rule's protection fully intact for every case it was actually written for: not yet settled, or settled with a mismatch.

**Non-Goals:**
- Not weakening `settlement-validation` itself, or dismiss_mismatch/the settlement-review card — those are a different flag on a different axis (is the *settlement* right), this change is about the *info-request* axis (did Justin's outstanding to-do get done).
- Not auto-confirming anything for a claim that ISN'T settled yet (`acknowledged`, `suspended` with no later settlement) — only settlement completing cleanly is the trigger.
- Not touching `awaiting_petcover_clarification` (settlement-clarification-email) or `dismiss_mismatch` — orthogonal states with their own resolution paths already.

## Decisions

**Trigger point: reuse settlement validation's own clean/mismatch determination, don't compute it twice — but "clean" is stricter than "`_validate_settlement` returned `None`".** `_validate_settlement` is called on EVERY `approved`/`settled` reply, and returns `None` in two different situations: (a) genuinely validated, no mismatch, and (b) nothing to validate at all — `paid_amount` was absent from that event's detail (a bare acknowledgement, or the dollar-less "payment processed" notice that follows an `approved` email). Confirmed by reading `_validate_settlement` directly (`claim_status.py:1372-1374`: `if paid_amount is None: return None`). Firing on either case would auto-confirm on a mere acknowledgement, long before any real settlement figure exists — wrong.

**"Clean" (see CONTEXT.md's new "Settled clean" glossary entry) requires all three:** `claim["status"] == "settled"`, the triggering event's `detail` carried a non-null `paid_amount`, and `_validate_settlement` returned `None` for that event. Only then does the hook look for any currently-unresolved `info_requested`/`suspended` event on the same claim and call `confirm_resolved` for it. A mismatch (or a dollar-less event) changes nothing — the existing manual-confirm rule stands exactly as before.

**Scope is "settled and clean," not "settled" alone.** The original rule's own reasoning ("a claim isn't silently dropped when Petcover's own follow-through is inconsistent") is exactly what a dirty settlement still represents — this change narrows the rule, it doesn't remove it. A claim that settles with a Check A/B mismatch keeps requiring Justin's explicit confirm, same as today.

**Applies regardless of `owed_by`.** Vet-owed, Justin-owed, or Petcover-owed (the new value from `vet-reply-auto-resolves-info-request`) — the settlement reconciling is evidence about the OUTCOME, not about which party the app last recorded as responsible for an intermediate step. Alternative considered: only auto-confirm `owed_by: justin` items (narrower, since those are Justin's own unconfirmed to-dos) — rejected as arbitrary: a vet-owed request that's still open when the claim settles cleanly is just as clearly moot as a Justin-owed one.

**Reuses `confirm_resolved` with a distinguishing detail, not a new writer.** Mirrors `vet-reply-auto-resolves-info-request`'s own pattern (`confirm_resolved(claim_id, detail=..., email_id=...)`) — one function, callers pass what triggered it. Auto-confirm here passes a detail noting it fired from clean settlement, not a tap or a matched reply.

## Risks / Trade-offs

- **A genuinely outstanding info request gets silently closed** because the settlement happened to validate clean for unrelated reasons (e.g. Petcover approved based on other information, the outstanding ask was actually still needed for something else Justin hasn't noticed yet). Accepted: Justin has explicitly weighed this against the noise cost and decided the settlement itself is sufficient evidence. If this proves wrong in practice, the event history (append-only) still shows exactly what was auto-confirmed and why, so it's recoverable, not silent.
- **Order-of-events edge case**: if settlement validation runs before the deadline anchor `treatment_date()` calculation would have expired the request anyway, this just closes it slightly earlier than the deadline would have — no real cost.

## Migration Plan

The ongoing hook (Decisions, above) is purely additive at the event level and only fires on a *future* `approved`/`settled` event or a replay — it does not retroactively scan existing claims. Justin confirmed (grilling session, 2026-08-14) he wants the existing backlog (claims like #13, already settled clean before this shipped) cleared as a **one-off process** once this change is live, not left to wait for a future replay.

**Backfill mechanics, as agreed:**
- A throwaway script, not a permanent endpoint — triggered directly by whoever is implementing this (via `docker exec` into the running app container, calling `claim_status` functions directly), never something Justin has to run himself, and deleted after use.
- Eligibility, checked against a claim's full event history (not just its latest event, since the same "was a real `paid_amount` actually validated" question applies retroactively): `status == "settled"`, at least one historical `approved`/`settled` event carried a non-null `paid_amount`, the claim's CURRENT `flag` is `None`, and it still has an unresolved `info_requested`/`suspended` event.
- **Dry-run first, always.** Print the candidate list before calling `confirm_resolved` on anything, and each entry must carry enough for Justin to actually understand *why* it qualifies, not just its id: claim id, pet, Petcover reference/Sr, what the outstanding info request was about (subject, `owed_by`, requested document if any), and the settlement figures that made it clean (claimed/paid/excess/age-contribution as recorded on the qualifying event). Justin reviews the list; only on his go-ahead does the script re-run with confirms applied.

## Open Questions

None outstanding — resolved via grilling session 2026-08-14 (see Migration Plan).
