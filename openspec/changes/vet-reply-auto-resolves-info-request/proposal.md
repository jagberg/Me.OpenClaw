## Why

Justin follows up with vets directly (outside the app) when Petcover's letter names a document the vet owes. Checking those replies today means him re-reading each one himself and deciding what it means — the exact manual work this session's Gmail check just did by hand for four claims, where the one real reply found ("all required clinic notes were sent to your insurance company several weeks ago") wasn't a flat yes/no: the vet says they've handled it, but towards Petcover, not towards us — a different next step (chase Petcover) than either "resolved" or "still owed by the vet." A reply's arrival isn't one signal, it's several different ones, and the app should tell them apart the same way Justin would reading it himself, not treat every reply as equivalent.

## What Changes

- New pipeline poller (mirrors `poll_petcover_status`'s shape) watches for a reply from a clinic email address that currently owes an open, unresolved vet-directed information request.
- Correlates the reply to the **specific claim** it answers by matching the clinic's email address to the claim(s) it currently owes, then narrowing by the claim's Petcover reference/Sr appearing in the reply's subject or thread (the same signal Justin's own follow-up subject lines already carry, confirmed live this session).
- Interprets the reply's content (`llm.extract()`, scoped tightly — free-form vet prose has no fixed template to match) into one of a small set of outcomes, and maps each to the state that already exists to represent it:
  - **Vet provided it** (attached it, or clearly states it's done on their end towards us) → resolved, via the existing `confirm_resolved` — the same action Justin's own tap already performs.
  - **Vet says they sent it to Petcover directly** → the claim is no longer waiting on the vet, it's waiting on Petcover confirming receipt — recorded via a new `info_requested` event with `owed_by: "petcover"` (extending the existing `owed_by` vocabulary — today's only values are `vet`/`justin`), which is Justin's own explicit next step if nothing follows ("I'll need to follow up with Petcover if nothing happens from this").
  - **Vet says they can't find it / declines** → stays owed by the vet, exactly as today, with a visible note of what they said rather than a silent no-op.
  - **Unclear or doesn't answer the question** → left completely untouched — never guessed, same discipline as every other classifier in this codebase.
- An ambiguous correlation (clinic owes two+ claims and the reply doesn't name which) is left open for Justin, same as today — this is a correlation failure, not a content one, and content is never interpreted for a claim the reply can't be attributed to.
- `gmail_ingest`'s task-capture poller must not swallow or permanently-mark these replies before this poller sees them (the exact trap that lost five Petcover approval letters — `processed_emails` is a shared dedupe gate, first poller to mark it wins).

## Capabilities

### Modified Capabilities
- `claim-status-tracking`: the "unanswered vet-directed request" lifecycle gains an automatic resolution path from a clinic's reply, and the `owed_by` vocabulary gains a third value (`petcover`) for the "vet says they've already sent it directly" outcome. Neither Petcover's own reply classification nor the core `info_requested`/`confirm_resolved` machinery changes.

## Impact

- `app/openclaw/pipeline.py`: new poller, called from `run_once` alongside `poll_petcover_status`.
- `app/openclaw/gmail_ingest.py`: exclude senders currently owing an open vet-request from task capture, without permanently marking them (mirrors the existing `PETCOVER_STATUS_SENDERS` carve-out) — narrower than excluding all `vet_contacts`, since most vet mail has nothing to do with an open request.
- `app/openclaw/claim_status.py`: correlation helper (clinic email + reference/Sr in subject/thread), the reply-classification `llm.extract()` call, and the new `owed_by: "petcover"` value's dashboard/Telegram label; no change to `confirm_resolved` itself or to how Petcover's own mail is classified.
- Justin's own explicit dashboard/Telegram "confirm resolved" remains available for every case this poller can't resolve unambiguously — this is additive, not a replacement.
