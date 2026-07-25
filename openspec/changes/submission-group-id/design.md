## Context

The **Submission** — up to 4 same-pet claims sharing one Petcover form, one Gmail draft, one email, one eventual claim reference — is already a real concept in the code, but it is modelled three times over and named nowhere:

| Where | How it groups | Verified |
|---|---|---|
| `pipeline._batch_key` (`pipeline.py:136`) | `draft_id`, else `claim-{id}`; `pending_match` claims group by `(merchant, flag)` instead | notification path, works |
| `claim_status._submission_key` (`claim_status.py:186`) | `draft_id`, else `claim-{id}` | correlation path, works |
| `claim_status.submissions_awaiting_reply` (`:867`) | `draft_id`, else `claim-{id}` | works |
| `agent._single_target` (`agent.py:176`) | collapses rows sharing one `draft_id` to the lowest id | works |
| **`claim_status.pending_actions` (`:776`)** | **doesn't group — one entry per claim** | **the defect** |

So the grouping rule is settled and repeated four times; the outstanding-work derivation is the one place that ignores it. `mark_sent` (`claim_status.py:447`) already does the right thing at the data layer — `UPDATE … WHERE draft_id = ?` — which is why Justin's data ended up correct despite the second tap being rejected.

Live state confirming the scale (read-only query, per `ADR-0018`):

```
draft_id            claims
19f6fdd2cf13fb75    18,19,21
19f8c0147e14f07a    8,22
19f9789889397dff    6,7      <- #6 + #7, both now 'sent'
```

Constraint that shapes the main decision: a live schema change to an existing table needs a hand-written `ALTER TABLE` against `app/data/openclaw.db` (`CREATE TABLE IF NOT EXISTS` in `db.py` won't touch it) — root `CLAUDE.md`. A new column is not free here.

## Goals / Non-Goals

**Goals:**
- One `/actions` card and one button per submission, so sending one email needs one tap.
- A short human-sayable id for a submission, usable on a card, in a push, and in chat.
- Keep the four existing grouping implementations authoritative — agree with them, don't add a fifth rule.
- No manual live DDL.

**Non-Goals:**
- Grouping the dashboard's per-claim list. Out of scope; the wording fix covers the confusion.
- A stored, immutable submission key. See Decision 1 and the re-batching risk.
- Collapsing any action kind other than `mark_sent`. See Decision 3.
- Teaching the chat agent to say the group id in prose. It inherits the collapsed entries automatically; prose is cosmetic.

## Decisions

### Decision 1 — the group id is derived from claim ids, not stored

`S<ids ascending, joined by +>`: `S6+7`, `S18+19+21`, and `S12` for a lone claim.

Rationale:
- **It contains what Justin already acts on.** Every command is by claim id (`/mark 6 …`, `/pet 1 …`) and a standing requirement says every message must carry `#N`. A group id built *from* those ids satisfies both at once; an opaque one would sit beside them as a second vocabulary.
- **No migration.** Derived means no column, no `ALTER TABLE` against the live DB, no backfill for the three existing batches.
- **Deterministic.** Ascending sort, so the same group always renders the same token regardless of query order. (Note: the batch PDF filename is `claim-batch-7-6.pdf` — batch-list order, descending here. That order is *not* the label's; don't reuse it.)

Alternatives considered:

- **A stored `submission_id` INTEGER sequence → `S12`.** Shorter, and stable even if a batch is re-split. Rejected *for now*: it costs a manual live `ALTER TABLE` plus a backfill, and buys stability against a re-batch operation that does not exist in the codebase. Becomes the right answer the moment one does — see Risks.
- **A short hash of `draft_id` → `S3fdf`.** Stable and fixed-width, but tells Justin nothing and can't be mapped back to a claim id without a lookup. Directly against "simple" as he asked for it.
- **Reuse the Petcover claim reference as the group id.** It *is* the natural submission identity and `_submission_label` already prefers it — but it only exists after Petcover's acknowledgement reply, and the whole problem occurs at `drafted`, before any reference exists. It stays the preferred *display* label once known; the group id is what fills the gap before then.

### Decision 2 — one shared helper, `claim_status.submission_group_id`

The grouping rule already exists four times. Add the *label* in one place and have the new code path use it; leave the four working call sites alone rather than refactoring them into it in this change (a no-behaviour-change refactor of the notification and correlation paths is a bigger blast radius than the bug warrants). The helper must produce a label consistent with `_batch_key`'s grouping for `drafted` claims — the state where both are live at once.

`pending_match` claims are the one case where `_batch_key` groups by `(merchant, flag)` rather than by draft. Those claims are never `mark_sent`-able, so the group id never applies to them; the helper is only defined for claims that have (or lack) a draft.

### Decision 3 — collapse only `mark_sent`

Of the nine action kinds in `ACTION_PRIORITY`, only `mark_sent` can fire on a batched claim:

- `assign_pet`, `set_condition`, `split_proposal`, `unmatch`, `invoice_request_sent`, `blocked_insurer` all fire at or before `matched` — no draft exists yet, so no group exists (`pipeline.py:236` says the same thing: "matched claims aren't batched").
- `confirm_resolved` and `dismiss_mismatch` are driven by `claim_status_events`, which correlate to a claim. Two claims of one batch *can* each carry an event, and a single Petcover letter about the batch could plausibly warrant one card — but that is a correlation question, not this bug, and collapsing it would hide a per-claim settlement mismatch behind a group. Left per-claim.

Encoding the set as a constant (e.g. `SUBMISSION_LEVEL_ACTIONS = ("mark_sent",)`) rather than inferring "has a draft_id" keeps that reasoning visible and reviewable.

### Decision 4 — the collapsed action keeps a single `claim_id` alongside `claim_ids`

`_action_keyboard` builds `callback_data=f"sent:{claim_id}"`, and `mark_sent(any_member_id)` already advances the whole group. So the collapsed entry carries `claim_id` = lowest member id (same convention as `agent._single_target`) plus `claim_ids` = the full list. Every existing consumer that reads `claim_id` keeps working unchanged; only the card text needs to know about `claim_ids`.

This is what makes the change small: the tap path, the callback token and the DB update all already handle batches correctly.

### Decision 5 — already-sent reports a no-op, not a rejection

`mark_sent` on a claim whose status is `sent` currently returns `Claim #7 isn't drafted (status: sent)`. When the claim shares a draft with claims that are `sent`, the honest message is that the submission was already marked sent, naming the group. `ok` stays `False` — nothing was changed, and `confirm_resolved` already sets the precedent of returning `ok: False` for a nothing-to-do call. Only the wording moves.

Scope limit: this applies to `sent` specifically. A claim at `acknowledged` or `settled` must not be reported as "already marked sent" — those advanced past sent through Petcover's replies, and flattening them would misdescribe the state.

## Risks / Trade-offs

- **A derived id changes if a batch is ever re-split** → No code path re-batches a drafted claim today; the only reset is `invoice_matching.unmatch` back to `pending_match`, which clears the batch entirely rather than re-grouping. This is load-bearing on the *open* question "What does 'redo claim #N' mean?" (`openspec/BACKLOG.md`) — whichever redo semantics Justin picks, if it can re-split a drafted batch then this token is no longer stable and Decision 1's rejected alternative (stored `submission_id`) becomes correct. Mitigation is to record the dependency, not to pre-build the column.
- **Token length grows with batch size** → 4 claims is the hard cap (Petcover's form row limit), so worst case is `S18+19+21+22` (12 chars). Fine on a Telegram card; noted so nobody assumes fixed width.
- **Collapsing changes the "N to action" count** → `/actions`' caption and `render_actions_summary`'s total must be computed from the same collapsed list, or the caption and the cards will disagree — a fresh instance of exactly the disagreement the shared-derivation requirement exists to prevent. Covered by a test asserting caption count == card count for a batch.
- **Old Telegram cards stay tappable forever** → A card pushed before this change still shows two buttons, and a stale tap will now hit the Decision 5 path. That is the point: the wording fix is what makes the residual case honest rather than confusing. No message-editing/retraction is attempted.
- **`draft_id` is overloaded** (claim drafts *and* invoice-request drafts — `app/openclaw/CLAUDE.md`) → The group id is only minted for claims reached via a `mark_sent` action, which requires `status == 'drafted'`; an invoice-request draft sits at `pending_match`. So the overload can't leak in here, but the helper must not be repurposed as a general "group any claims by draft_id" utility without re-checking it.

## Consequences found in review (not in the original plan)

Both were missed when this change was proposed and caught auditing the trail afterwards. Recorded here rather than quietly fixed, because each changes behaviour a requirement already depends on.

- **The chat agent would have dropped the other claim ids.** `agent.pending_actions`'s wrapper renders `f"#{a['claim_id']} …"`. With the collapse in place that prints `#6` for the `S6+7` submission and omits `#7` — from the answer describing that batch. Fixed by rendering the group id plus every member id, and specced as its own requirement, because "every claim reference carries its id" is only satisfied when every referenced claim's id appears.
- **Date filtering over a batch changed meaning.** `agent.pending_actions(since, until)` filters on `a["date"]`. That used to be each claim's own transaction date; a collapsed entry's date is now its *oldest* member's. So a batch whose members straddle a range edge is matched once, on the oldest date, instead of appearing once per in-range member. That is the better answer — a submission is one thing to act on — but it is a change, not a no-op, and "what actions do I have for July 2025" can now surface a submission containing an August claim. Specced as a scenario so it is a decision rather than an accident.

## Open Questions

- Should the group id be **typeable**, i.e. should `/sent S6+7` work as well as `/sent 6`? Not proposed — nothing asked for it, and `mark_sent(6)` already does the group. Left unbuilt rather than guessed at.
- Should a single Petcover letter about a batch produce one `confirm_resolved` card instead of one per claim? Genuinely unresolved (Decision 3); needs a real batched info-request to reason from, and none is in the data yet.
