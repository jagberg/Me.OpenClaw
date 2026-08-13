## Why

Claim #13 sits on the dashboard/Telegram needing "Confirm Resolved" for an old `info_requested` event (`owed_by: justin`), even though the claim is `settled` and its numbers reconcile exactly — Petcover assessed $944.50, matching our recorded claimable subtotal to the cent, and paid $516.42 after the stated excess and age contribution. Whatever Petcover needed from Justin evidently arrived, or they wouldn't have approved and paid it. Justin's own read on seeing this: "if it's paid out and the numbers are right it should close it off. I don't need to do any further action" — nagging him to confirm something the settlement itself already proves happened is noise, not a safeguard.

This directly narrows an existing, deliberate rule (`claim-status-tracking`: "A new event arriving on a claim — even settled or declined — SHALL NOT automatically clear its needs-action status"), written after a real pattern of repeated, unresolved "request for X" emails on the same claim. That reasoning still holds for a claim that settles WITHOUT clean numbers, or hasn't settled at all — this change only relaxes it for the specific case where settlement has actually completed and validated clean.

## What Changes

- When a claim reaches `settled` and its own settlement validation (Check A + Check B, `settlement-validation`) finds no mismatch — the same "clean" determination already computed there, not a new check — any outstanding `info_requested`/`suspended` event on that claim is auto-confirmed via the existing `claim_status.confirm_resolved`, the same path Justin's own tap uses. Not a new resolution mechanism.
- A claim that settles WITH a mismatch (Check A or Check B flags it) is unaffected — the existing manual-confirm rule still holds, since "settled" alone was never the safeguard the original rule protected; "settled and clean" is what's new.
- This applies regardless of who the outstanding request was owed by (vet, Justin, or Petcover per `vet-reply-auto-resolves-info-request`'s new `owed_by: "petcover"` value) — the settlement itself is the evidence, not which party the app last recorded as owing something.
- No change to how `info_requested`/`suspended` behave for a claim that ISN'T yet settled, or settles uncleanly — the original rule's protection stays exactly as strong there.

## Capabilities

### Modified Capabilities
- `claim-status-tracking`: "An info-requested or suspended claim stays flagged until Justin explicitly confirms it resolved" gains a narrow, explicit exception — settled AND validated clean auto-confirms; every other case is unchanged.

## Impact

- `app/openclaw/claim_status.py`: hook at the point settlement validation already determines clean-vs-mismatch for an approved/settled event (`_validate_settlement` or its caller) — on a clean result, auto-call `confirm_resolved` for any outstanding info_requested/suspended event on the same claim.
- No new event type, no new writer — reuses `confirm_resolved` exactly, with a detail flag distinguishing "auto-confirmed on clean settlement" from Justin's own tap (mirrors how `vet-reply-auto-resolves-info-request` already records its own auto-confirms distinctly).
- `openspec/specs/claim-status-tracking/spec.md`: the existing requirement's text is amended in place (MODIFIED, not superseded) to state the exception, with the original reasoning kept alongside it.
