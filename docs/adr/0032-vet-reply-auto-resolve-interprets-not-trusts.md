# ADR 0032: A vet clinic's reply is interpreted into one of four outcomes, not treated as blanket confirmation

- Status: accepted
- Date: 2026-08-13

## Context

`claim_status.unanswered_vet_requests()` lists claims with an open, unresolved information request owed by a vet clinic (Petcover asked for a document, the clinic owes it). Justin chases these by his own email, outside the app, so the app had zero visibility into that conversation. A live Gmail check this session (four open requests) found one real reply, from Kingsgrove Animal Hospital: *"All required clinic notes were sent to your insurance company several weeks ago."*

The first draft of this change (`vet-reply-auto-resolves-info-request`'s original proposal) treated **any reply from the owing clinic as sufficient** to auto-confirm the request resolved, on Justin's own stated bar ("if the vet replies then assume they have"). Reviewing the one real reply against that rule exposed the problem directly: calling it "resolved" would have been wrong. The vet's job is done, but towards **Petcover**, not towards us — nothing confirms Petcover actually received it, and the honest next step is Justin chasing *Petcover*, not closing the question. Justin's own words on seeing it: he'd need to follow up with Petcover if nothing happens next — which is a different state than "resolved."

## Decision

A matched reply's content is classified (via one new, tightly-scoped `llm.extract()` call — `claim_status._classify_vet_reply`) into exactly one of four outcomes, each mapped onto state vocabulary the app already has rather than a new mechanism:

- **`provided`** → `claim_status.confirm_resolved` — the existing manual-tap path, unchanged, just given a new caller.
- **`sent_to_petcover`** → a new `info_requested` event with `owed_by: "petcover"`, a **third value** added to the existing two (`vet`/`justin`). The claim is not resolved; it now means "Justin needs to confirm with Petcover, not chase the vet again."
- **`unavailable`** → the request stays owed by the vet, exactly as before, but the vet's stated reason is recorded visibly (a flag note) rather than silently dropped.
- **`unclear`** → nothing happens. Same discipline as every other classifier in this codebase: an unconfident read is not evidence of anything.

Correlation (which specific claim a reply answers, when one clinic owes several at once — confirmed live: Kings Vet owed both claim #6 and claim #8 simultaneously) happens **before** content is interpreted, by matching `(petcover_reference, petcover_sr)` in the reply's subject/thread. Zero or multiple matches mean nothing is touched, and content is never interpreted for a claim the reply can't be attributed to.

## Alternatives considered

- **Any reply from the owing clinic resolves the request (this change's own first draft).** Rejected mid-session: the one real reply found live is exactly the case it would have gotten wrong — a vet saying "we sent it to Petcover" is not the same fact as "Petcover has it," and treating them alike hides exactly the follow-up Justin said he'd still need to do.
- **A second, parallel "chase Petcover" state machine for the `sent_to_petcover` case**, separate from `owed_by`. Rejected: `owed_by` already exists to answer "who does Justin need to act on" (this file's own repeating gotcha: "never default it — naming the wrong party is how the chase never happens"); a third value is the smaller addition, and every existing reader keyed on `owed_by` (labels, the vet-nudge list) gets the right behaviour for free once the value exists, rather than needing a second field taught to every reader.
- **Verifying receipt with Petcover automatically** (e.g. cross-checking a later Petcover letter). Rejected for this change: explicitly left as Justin's own manual follow-up. Nothing here closes that loop.

## Consequences

- `TRANSITIONS["info_requested"]` gained a **self-loop** (`info_requested` → `info_requested`) as part of this change — not a poller-only special case, but a real pre-existing gap: claim #8's actual event log holds `acknowledged → info_requested → info_requested` (a second Petcover request letter arriving while the first was unresolved, 2026-07-29), which was being silently refused and flagged before this fix. This bug predates the feature; the feature is what exposed it, because `sent_to_petcover`'s common case is recording a second `info_requested` event on a claim already sitting at that status.
- Two `owed_by` read sites the original proposal didn't name — `claim_status._ACTION_META["confirm_resolved"]`'s waiting-party dict and `_waiting_key` — were found defaulting anything not `"vet"` to `"justin"`'s wording. Both now branch on `"petcover"` explicitly. Any *future* `owed_by` read site must do the same; there is no test enforcing this (unlike, say, the one-status-vocabulary guard) — a convention, not a mechanical guard.
- The classifier can misread `provided` vs. `sent_to_petcover`. Accepted: neither writes anything money-affecting, and a wrongly-`sent_to_petcover` claim just sits visibly waiting until Justin looks — cheaper to be wrong about than a resolved-and-forgotten claim would be.
- A clinic emailing about something unrelated to an open request is still captured as a task normally — the `gmail_ingest` exclusion is scoped to "currently owes an open request," recomputed every poll, not a static list of every known vet contact.
