## Context

`vet_claims.status` is doing two jobs. It is the pipeline's state machine (`pending_match → matched → drafted → sent → acknowledged → approved → settled`, with `info_requested` / `suspended` / `declined` / `below_excess` / `absorbed` off to the side), and it is also the word Justin reads on the dashboard, in `/basic`, on Telegram cards and in notify messages. Those two jobs disagree, and the disagreement is now measurable:

- `matched` is a precise pipeline state and useless as a label. All seven live `matched` claims are Echo's, permanently blocked on `Bow Wow Insurance claim process not yet defined`, and they read exactly like a claim that needs a condition typed in.
- The wording lives in three hand-synced copies (`claim_card._STATUS_LABELS`, `templates/index.html`'s `labels`, `templates/basic.html`'s `needs`) plus `pipeline`'s notify strings. `claim_card`'s only link to the others is the comment "Mirrors templates/index.html's status_chip macro."

What this change does **not** cover, because it is already done or already owned:

- Classification (`info_requested` ahead of `suspended`, `further information required`, sender-based classification for `requiredinfo.au@`), Unicode-hyphen normalization, and addressee resolution from `To:`/`Cc:` against `vet_contacts` all shipped in `4a7fb6d` (`vet-info-request-chase` groups 2–4).
- The `chase_vet` action kind, deadline escalation, the `info_requests` register, the backfill and the new dashboard/Telegram surfaces are that change's groups 5–8.
- Claim #2's stored reference and #2/#8's stored `suspended` status are its groups 0–1. Confirmed still uncorrected read-only on 2026-07-28: zero `info_requested` events, claim #2 `petcover_reference = 'DC1'`, both claims `suspended`. **Relabelling does not touch stored rows** — those two claims keep reading as suspensions until that repair happens, and the design here must not be mistaken for fixing them.

Constraints:

- Live schema changes to existing tables need a hand-run `ALTER TABLE`. This design must not need one.
- ADR-0008: `claim_status_events` is append-only, needs-action persists until Justin's explicit confirm-resolved click. Nothing here writes to either.
- `vet-info-request-chase` group 5 edits `claim_card.py` and `pipeline.py`, which this change also edits.

## Goals / Non-Goals

**Goals:**

- A `matched` claim says what it is waiting for.
- One label map, one place to change it, and the next new state costs one edit instead of three.
- No schema change, no stored-status rewrite, no new LLM call.

**Non-Goals:**

- Renaming stored `status` values. `suspended` stays `suspended` in the DB, in `AWAITING_REPLY_STATUSES`, in the event log and in every existing query. This is vocabulary at the boundary, not identifiers.
- Anything in `vet-info-request-chase`'s scope (see Context).
- Restyling. Colours and severity classes stay as they are; only their **keys** change from label to status where they currently key off wording.

## Decisions

### 1. Status is stored state; label is who-holds-it. New module `openclaw/status_labels.py`.

`label(claim) -> str` takes a claim row (or the mapping the ledger already builds) and returns the display string; `LABELS: dict[str, str]` stays available for the status-only case (`claim_card`'s legend, `/history` rows). The module imports nothing from `claim_status` — it is a pure function of the row — so `claim_status`, `claim_card`, `pipeline` and both templates can all read it without an import cycle.

Alternatives: a Jinja filter (leaves Telegram cards and notify text out); a `@property` on a claim object (there is no claim object — rows are `sqlite3.Row` everywhere); renaming the stored statuses (touches ~40 call sites and the event log for a cosmetic win).

### 2. `matched`'s label is derived from the outstanding action, not from a second stored field.

`claim_status._action_kind` already answers "what does this claim need" — `set_condition`, `assign_pet`, `blocked_insurer` — from the row's own `status`/`flag`/`pet_id`/`condition_text`. The label map for a `matched` row reads that answer:

| `_action_kind` | label |
|---|---|
| `set_condition` | Needs condition |
| `assign_pet` | Needs pet |
| `blocked_insurer` | Blocked: no claim process |
| anything else / none | Matched |

`_action_kind`'s two set-valued arguments (`open_split_claim_ids`, `unresolved_event_claim_ids`) exist for kinds that don't apply to a `matched` row, so `status_labels` calls the pure part with empty sets rather than dragging DB queries into a rendering path. To keep that honest the pure predicates move into a small helper `_action_kind_from_row(claim)` that `_action_kind` calls after its two set checks — one function, one definition of "what this claim needs", no second copy to rot.

Alternative: a `blocked` status stored on the row. Needs DDL, needs every state-machine consumer to learn it, and duplicates what `flag` already says.

### 3. An information request is worded by who owes the document.

Justin's wording: **More vet info required**. It is right for the case he saw, but not for every case — the audit of 2026-07-28 found both copies of the same letter in the log, one owed by the vet (event 28, `info@kingsvet.com.au`) and one owed by Justin himself (event 27, addressed to him). `resolve_owed_by` already distinguishes them, so the label does too:

| `owed_by` | label |
|---|---|
| `vet` | More vet info required |
| `justin` | Petcover needs info from you |
| unrecorded | Info requested |

Never defaulting to "vet" mirrors `resolve_owed_by`'s own rule (it never defaults to Justin, because reassigning a vet's obligation is what loses a claim — the same argument runs the other way for the label: telling Justin the vet owes it when he does is the same loss).

### 4. Colour and severity tables key off status, not label.

`claim_card._STATUS_COLOURS` is keyed by *label* today (`"Info requested"`, `"Below excess"`), so renaming a label silently drops a colour to the default. Re-key it by status. `basic.html`'s `sev` map is already status-keyed and stays put — severity is presentation, and a designer changing a chip colour should not have to reason about the vocabulary.

### 5. Sequencing with `vet-info-request-chase`, stated rather than discovered.

Both changes edit `claim_card.py` and `pipeline.py`. Two orders, both fine:

- **This change first** (preferred — it is small, has no live-data dependency, and its group 0 blocker doesn't apply): `vet-info-request-chase` 5.5 then adds `chase_vet` as one entry in `LABELS` instead of a label in `claim_card.py` plus another in `telegram_bot.py`. Task 5.5's wording should be amended to say so.
- **That change's group 5 first**: this change absorbs its two `chase_vet` labels into the map as part of task 3.3/3.4 below, and deletes them from the renderers. Nothing is lost either way; only the diff moves.

Not merged into `vet-info-request-chase` because that change is mid-flight with a live blocker (its group 0 regressed four claims and is unbuilt), and the vocabulary has no dependency on any of it. Coupling a small, safe, deployable change to a blocked one delays it for nothing.

## Risks / Trade-offs

- **Derived labels make the dashboard's word and the DB's `status` differ** → the claim detail view keeps showing the raw status, and the ADR states the split explicitly so the next reader doesn't "fix" one to match the other.
- **A duplicate label map reappears later** → task 3.6 asserts that both `claim_card` and the ledger resolve a chosen status through `status_labels`; the ADR names the three maps this change deleted, so a fourth reads as a regression rather than a convention.
- **Merge conflict with `vet-info-request-chase` group 5** in `claim_card.py`/`pipeline.py` → both changes now name the collision and the resolution (Decision 5). The conflict is a handful of lines in a label table, not a design clash.
- **Justin's dashboard still shows two false suspensions after this ships.** Nothing here can fix that. Stated in the Context, in the proposal, and in task 4.4 as the thing to report rather than quietly leave.

## Migration Plan

1. Ship code + tests; no DDL, no data migration. Existing rows render under the new vocabulary immediately.
2. Deploy with `./scripts/deploy.ps1` from the worktree (stamps `APP_VERSION`).
3. Verify live: the seven Echo claims (#3, #9, #10, #15, #16, #20, #25) read **Blocked: no claim process** on both the dashboard and `/basic`, and a rendered history card agrees word-for-word.

Rollback is a revert: nothing persisted changes shape.

## Open Questions

- Wording for the blocked case: **Blocked: no claim process** is chosen over "Blocked" alone because the flag text is already visible next to it and the chip should stand alone in the Telegram card, where the flag is not. One map, trivially changed.
- Whether `pending_actions`' `_ACTION_META` titles ("Set condition", "Assign pet") and these labels should be the same strings. They read the same determination but answer different questions ("what do I do" vs "where is this claim"), so they stay separate for now — worth revisiting if they drift.
