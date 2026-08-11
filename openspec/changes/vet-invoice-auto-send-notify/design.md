## Context

`invoice_matching.draft_invoice_request` creates a Gmail draft
(`drafts().create()`) and `pipeline._maybe_draft_invoice_request` stores its
message id in `vet_claims.draft_id`, flag `invoice_request_drafted`. Justin
finds the draft, reviews, sends himself, then either clicks the dashboard's
"Invoice-request sent" button (`/claims/{id}/invoice-request-sent`) or the
Telegram agent's `reconcile_sent_invoice_requests` tool detects the send via
Gmail's SENT label and sets `invoice_request_sent_at` automatically.

`pipeline.notify_claim_states`'s query explicitly excludes
`flag = 'invoice_request_drafted'` from the `pending_match` notify branch —
correct today, since a fresh draft is nothing Justin can act on yet.

Justin has explicitly overridden CLAUDE.md's "never send email" hard rule for
this one call site only (2026-08-11, this conversation) — confidence in the
drafted body is now high enough that manual review before sending buys
nothing. This design assumes that override is granted; the ADR recording it
is written alongside this change, not as a task inside it.

## Goals / Non-Goals

**Goals:**
- Send the invoice-request email directly via Gmail's `send()`, keeping the
  existing vet-email lookup and body template unchanged.
- Notify Justin on Telegram exactly once per claim/batch when the send
  happens, using the existing `notify_claim_states` grouping/dedupe machinery
  rather than a new notification path.
- Leave every other Gmail interaction (matching, Petcover reply ingestion)
  exactly as restrictive as today — this is not a general send capability.

**Non-Goals:**
- No change to how claims already sitting at `invoice_request_drafted` (from
  before this change ships) are handled — they stay Justin's to send
  manually; `reconcile_sent_invoice_requests` keeps polling for them.
- No new Telegram command or button for this flag — it is a pure
  notification, there is nothing to tap.
- Not touching the Petcover-side send path or any other email type. If a
  future change wants to auto-send something else, it needs its own decision,
  not a precedent read off this one.

## Decisions

**Rename, don't parameterize.** `draft_invoice_request` becomes
`send_invoice_request`, calling `service.users().messages().send()` instead of
`.drafts().create()`. Considered keeping one function with a `send: bool`
flag — rejected: the draft path is going away for new claims entirely (the
"send anyway, but discard vs. keep" fork does not exist here), and a
boolean parameter invites a future caller to pass `send=False` and reintroduce
the very ambiguity CLAUDE.md's rule existed to prevent. The function name
itself should be the guard.

**No new `draft_id`-shaped column for the sent message id.** The Gmail
`send()` response also carries a message id. Storing it in the existing
`draft_id` column was considered and rejected — a column literally named
`draft_id` holding a *sent* message's id is exactly the kind of stale-name
trap CLAUDE.md's data-dir section already warns about elsewhere in this repo.
Nothing currently reads a sent-message id back, so it is not persisted at all;
`draft_id` stays `NULL` for claims that go through the new path (this is also
what keeps `reconcile_sent_invoice_requests` and the dashboard's manual
"sent" button correctly inert for them, see below — no code change needed in
either).

**`invoice_request_sent_at` is set synchronously, at send time.** Unlike the
manual-draft path (which cannot know when Justin sends it, hence the
poll/click), the app knows the instant `send()` returns 200. Setting it
immediately means `reconcile_sent_invoice_requests`'s query
(`invoice_request_sent_at IS NULL AND draft_id IS NOT NULL`) and the
dashboard's `flag == 'invoice_request_drafted'` template check both simply
never match rows created by the new path — no exclusion logic to add, they
were already scoped narrowly enough.

**The new flag value, not a query change, is what makes
`notify_claim_states` fire.** Today's exclusion is a literal string match:
`flag != 'invoice_request_drafted'`. Using a *different* literal —
`invoice_request_auto_sent` — for the new path's flag means the existing
`pending_match` OR-clause already matches it with zero changes to that SQL.
This is deliberate but fragile-looking, so the flag constant gets a code
comment at both ends (where it's set in `pipeline.py`, and next to the
exclusion list) stating plainly that this string is relied on to *not* be
excluded — so a future "clean up the noise flags" pass doesn't add it to the
exclusion list and silently kill the notification.

The literal is `invoice_request_auto_sent`, not the shorter `invoice_request_sent`
originally planned — `claim_status.py`'s `_action_kind_from_row` already uses the
bare string `"invoice_request_sent"` as an *action-kind* label (for the legacy
manual-confirm flow: "did you send the drafted request yet?"). Same text, two
unrelated meanings would have been a real trap for the next grep. No functional
collision exists either way — the action-kind check keys off `flag ==
"invoice_request_drafted"`, not off this new value — but the near-miss was close
enough to rename rather than footnote.

**`_summarize_group` gets an explicit branch for this flag**, rendered
without the `⚠` prefix the generic `pending_match` branch uses (that prefix
is correct for warnings like "no vet email on file", wrong for good news):
`"✅ {merchant}: invoice request sent — ${amount} ({date})"`. No button
(`markup`) is attached — same as today, since none of the existing `elif`
branches match `status == 'pending_match'`.

**Send failure is a visible flag, not a silent no-op** (CLAUDE.md hard rule).
`_maybe_send_invoice_request` wraps the `send()` call; on any exception the
flag becomes `f"invoice request send failed: {exc}"`, mirroring the existing
"no vet email on file" pattern — this string does not equal
`invoice_request_auto_sent`, so it participates in the *existing* generic
`pending_match` exclusion-list logic and does surface with the `⚠` verbatim
rendering, which is correct (it's a real problem needing Justin's attention).

## Risks / Trade-offs

- **[Risk] A vet-email lookup mistake now sends a real email instead of
  creating a discardable draft.** → Mitigation: the lookup logic
  (`_lookup_vet_email`) is unchanged by this proposal — no new risk is
  introduced there, and the override was scoped by Justin specifically
  because this logic is already trusted. Not a reason to add a
  human-in-the-loop step back in (that's the friction being removed), but
  worth naming: this is the one place a bug in `_lookup_vet_email` now has a
  real-world side effect instead of a silently-wrong draft.
- **[Risk] The string-based notify-trigger coupling (relying on a flag value
  not being in an exclusion list) is easy to break silently.** → Mitigation:
  code comments at both ends (see Decisions); also covered by a regression
  test asserting `invoice_request_auto_sent` claims produce a `notify_claim_states`
  call.
- **[Trade-off] No audit trail of the sent message id.** Accepted per the
  "no new column" decision above — if a future need arises (e.g. "did we
  really send this"), Gmail's own Sent folder is the source of truth, same as
  it always has been for anything this app doesn't poll back.

## Migration Plan

- No schema migration: no new columns, no backfill. Existing
  `invoice_request_drafted` rows are untouched and continue through the
  existing manual-send path.
- Rollout is a plain code deploy; nothing to sequence or feature-flag — the
  new path only takes effect for claims that reach `_maybe_send_invoice_request`
  after deploy (i.e. new claims crossing `INVOICE_MATCH_WINDOW_DAYS` with no
  existing draft/sent record).
- Rollback: revert the code change. No data was reshaped, so this is safe at
  any point.

## Open Questions

None — Justin resolved the one open question (whether to override the
never-send rule, and how narrowly) directly in conversation before this
design was written.
