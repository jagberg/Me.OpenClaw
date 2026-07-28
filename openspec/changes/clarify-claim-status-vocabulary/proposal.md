## Why

Justin asked two things: stop showing **Suspended** when Petcover is only asking for a document, and explain why **Matched** never progresses.

The first is already fixed in code. `vet-info-request-chase` (merged to master as `4a7fb6d`, groups 2–4 done) reordered `info_requested` ahead of `suspended`, added the live phrases including `further information required`, classifies anything from `requiredinfo.au@` on the sender alone, normalizes the U+2010 hyphens that truncated `DC1‑26‑5992` to `DC1`, and records who owes the document from `To:`/`Cc:` matched against `vet_contacts`. That work is not repeated here. Its remaining groups (0, 5–8) add a `chase_vet` action kind, the deadline escalation, the register and the surfaces.

What is left, and what this change is for:

**The label is not the status, and nothing owns the labels.** Four surfaces render a claim's state and three of them carry their own copy of the wording — `claim_card._STATUS_LABELS` (commented "Mirrors templates/index.html's status_chip macro" — sync by hand), `templates/index.html`'s `labels` dict, `templates/basic.html`'s `needs` map — plus `pipeline`'s notify text. `vet-info-request-chase` task 5.5 is about to add a fourth label in two of those places for `chase_vet`, and 8.3 a fifth on the action cards. Every new state costs three edits, and the only thing holding them together is a comment.

**"Matched" tells Justin what the software did, not who holds the claim.** All seven live `matched` claims are Echo's, every one flagged `Bow Wow Insurance claim process not yet defined` — permanently blocked, no tap can clear them. They render identically to a claim that needs one condition typed in. That is his second question, and nothing in the merged change touches it: `matched` *does* progress (`matched → drafted` once a condition exists), but the word can't distinguish "waiting five minutes on you" from "can never proceed".

**Correcting the code did not correct the two claims he is looking at.** Verified read-only just now: the live DB still holds zero `info_requested` events, both #2 and #8 still `suspended`, and claim #2 still carries the truncated reference `DC1`. Those emails are already in the processed set, so only a re-read or a manual repair fixes them — `vet-info-request-chase` groups 0 and 1, where the re-read is currently *blocked* because a live trial regressed four claims. Restated here so nobody assumes relabelling will clear his dashboard.

## What Changes

- **One display vocabulary, defined once.** New `openclaw/status_labels.py` holds the single status→wording map plus a `label(claim)` function. `claim_card`, `templates/index.html`, `templates/basic.html` and `pipeline`'s notify text all read it; the three duplicate maps and the "mirrors index.html" comment go. Colour and severity tables stay where they are (they are presentation, not wording) but key off the **status**, not the label, so a rewording cannot break a colour.
- **A `matched` claim's label says what it is waiting for**: **Needs condition**, **Needs pet**, **Blocked: no claim process**, or plain **Matched** when nothing is outstanding. Derived from the same determination `pending_actions` already makes — `claim_status._action_kind` — not from a new stored field. No schema change.
- **The vocabulary is where `chase_vet` gets its wording**, so `vet-info-request-chase` 5.5/8.3 add one entry to one map instead of a label in `claim_card.py` and another in `telegram_bot.py`. Sequencing is stated in `design.md`: if that change lands its group 5 first, this change absorbs its two labels rather than duplicating them.
- Not in scope: classification, reference extraction, addressee resolution, the `chase_vet` action kind itself, the deadline escalation, the register, the claim #2 repair. All of those belong to `vet-info-request-chase` and several are already done there.

## Capabilities

### New Capabilities
- `claim-status-vocabulary`: the single source of truth for how a claim's state is worded to Justin — one label per status, derived labels for a `matched` claim that is really blocked or waiting on him, and the rule that every surface (dashboard, `/basic`, Telegram card, notify message) reads that one map rather than its own copy.

### Modified Capabilities
- `dashboard-visit-ledger`: status chips and the `/basic` "what's needed" line come from the shared vocabulary, including the derived `matched` labels.
- `telegram-bot`: rendered claim cards and lifecycle notification text take their state wording from the shared vocabulary rather than a renderer-local copy.

## Impact

- Code: new `openclaw/status_labels.py`; `openclaw/claim_status.py` (extract the pure part of `_action_kind` so the label can reuse it); `openclaw/claim_card.py`, `openclaw/pipeline.py`, `templates/index.html`, `templates/basic.html`.
- Data: **none.** No schema change, no stored-status change, no event rewritten. Existing rows render under the new vocabulary immediately.
- Tests: `app/tests/test_core.py` — label derivation for each `matched` case, and a guard that no second status→wording map has reappeared.
- Docs: ADR (status is stored pipeline state, label is who-holds-it), `app/openclaw/CLAUDE.md` (labels live in one module).
- Coordination: touches `claim_card.py` and `pipeline.py`, which `vet-info-request-chase` group 5 also touches. Land whichever first and fold the other's labels into the map — noted in both changes.
- No third-party calls, no LLM.
