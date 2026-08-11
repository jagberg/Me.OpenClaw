## Why

Justin drafts, reviews and manually sends every vet invoice-request email himself
(CLAUDE.md hard rule, never-send). Confidence in the drafted content is now high
enough that this manual step is pure friction — Justin has explicitly overridden
the never-send rule for this one call site (invoice-request emails only) so the
app can send it directly and tell him afterward, instead of waiting for him to
find, review and send the draft. This is also why nothing tells him a claim just
got a new action-worthy state after a CSV upload triggers the scan: an
`invoice_request_drafted` flag is deliberately excluded from Telegram
notification today as "noise, not an action" (nothing for him to tap). Once the
app sends the email itself there's a real event worth a notification.

## What Changes

- **BREAKING (narrow, deliberate):** `invoice_matching.draft_invoice_request`
  becomes `send_invoice_request` — it calls Gmail's `messages().send()` instead
  of `drafts().create()`. This is the one and only call site permitted to call
  `send()`; the CLAUDE.md hard rule is amended to scope "never send" to
  everything except this path, not removed.
- `vet_claims.flag` gains `invoice_request_auto_sent` (replacing
  `invoice_request_drafted` as the flag `_maybe_draft_invoice_request` — renamed
  `_maybe_send_invoice_request` — sets after a successful send).
- `pipeline.notify_claim_states` sends a Telegram notification when a claim's
  flag becomes `invoice_request_auto_sent` (dropping the current "drafted flags are
  noise" exclusion for this specific flag) — one message per claim/batch, same
  grouping as existing notifications.
- `pipeline.reconcile_sent_invoice_requests` and the dashboard's manual
  "Invoice-request sent" button are left unmodified — both already scope to
  rows with `draft_id IS NOT NULL` / `flag == 'invoice_request_drafted'`, which
  a claim created by the new send path never has (no draft is ever created), so
  they naturally stop matching new claims without a code change. They keep
  working for whatever legacy `invoice_request_drafted` backlog exists at
  cutover.
- CLAUDE.md's hard-rule bullet and `gmail-isolation-boundary`'s spec rationale
  (which currently states "'never send' is enforced only by the absence of
  `send()`") are updated to name the one exception explicitly, so a future
  session doesn't read the old absolute wording and think this is a bug.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `invoice-matching`: the invoice-request-email requirement changes from
  "drafts only, never sends" to "sends directly for this one email type"; the
  vet-email lookup and body-templating logic are unchanged.
- `telegram-bot`: `notify_claim_states` gains a new notify-worthy condition
  (flag transitions to `invoice_request_auto_sent`) where one previously existed as
  an explicit exclusion.

## Impact

- **Code:** `app/openclaw/invoice_matching.py` (`draft_invoice_request` →
  `send_invoice_request`), `app/openclaw/pipeline.py` (`_maybe_draft_invoice_request`
  rename + flag value, `notify_claim_states` condition, removal of
  `reconcile_sent_invoice_requests` and its caller), dashboard template/route and
  Telegram command handling for the old "mark sent" action on this flag.
- **Docs:** `CLAUDE.md` hard-rule bullet, `docs/adr/` (new ADR recording the
  override — hard to reverse, contradicts a previously non-negotiable rule,
  genuine trade-off), `gmail-isolation-boundary` spec rationale text.
- **Data:** existing `vet_claims` rows with `flag = 'invoice_request_drafted'`
  and a non-null `draft_id` are claims where a draft genuinely exists in Gmail
  from before this change — they are left alone (still Justin's to send
  manually); the new send path only applies going forward.
- **No change** to `gmail-isolation-boundary`'s credential-isolation
  requirements (gateway still holds no Gmail credential) — only the "never
  calls `send()`" assumption inside the Python process narrows.
