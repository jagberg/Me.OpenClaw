# ADR 0030: Vet invoice-request emails are sent directly, not drafted — a narrow, named exception to "never send email"

- Status: accepted
- Date: 2026-08-11

## Context

CLAUDE.md's hard rules state, unconditionally until now: "Never send email.
Gmail drafts only — `drafts().create`/`update`, never `send()`. Justin
reviews and sends himself." `gmail-isolation-boundary`'s spec Purpose section
rests its entire rationale on this being true: "'Never send email' is
enforced today only by the absence of `send()` in `app/openclaw/`. That
guarantee is code-shaped, so it holds only while the code with the credential
is the only code that can reach it."

`invoice_matching.draft_invoice_request` asks the vet for a missing invoice
once a `pending_match` transaction ages past the match window. Justin has
reviewed enough of these drafts to trust the body template and the
`vet_contacts`/matched-email address lookup unconditionally — the manual
find-review-send step buys nothing and is pure friction, especially now that
`csv-upload-via-telegram` (2026-08-10) can trigger a claim scan Justin isn't
watching in real time. He asked, directly, in conversation on 2026-08-11, to
override the never-send rule for this one email type, and to be notified on
Telegram once it's sent instead of having to find a draft.

## Decision

`invoice_matching`'s invoice-request-to-vet path is the one and only
permitted call site for Gmail `send()` in this codebase. Every other Gmail
interaction — matching, Petcover reply ingestion, everything else — remains
draft-only or read-only exactly as before. The hard rule is not removed; it
is narrowed to name its one exception, in CLAUDE.md and in
`gmail-isolation-boundary`'s spec text (amended in place, not superseded —
the boundary's actual requirements, credential isolation and no agent tool
reach, are untouched).

See `openspec/changes/vet-invoice-auto-send-notify/` for the full
proposal/design/specs/tasks.

## Alternatives considered

- **Keep drafting, just notify when a draft is created.** Rejected: this was
  the original ask this session started from, before Justin overrode it
  directly — it doesn't remove the friction he identified (he still has to
  find and send the draft himself).
- **A `send: bool` parameter on the existing function, defaulting to
  `False`.** Rejected in design.md: a boolean invites a future caller to
  pass `send=True` somewhere else and silently widen the exception. Renaming
  the function (`send_invoice_request`) makes the call site itself the
  guard — there is no parameter to misuse.
- **Widen the exception to "any transactional email, case by case."**
  Rejected: not asked for, and CLAUDE.md's hard rules exist precisely to
  avoid re-litigating this decision per call site. Scope is exactly one email
  type.

## Consequences

- A bug in `_lookup_vet_email` (unchanged by this decision) now has a
  real-world side effect — a real email sent to a real address — instead of
  producing a silently-wrong draft Justin would have caught before sending.
  Accepted: that lookup logic is exactly what Justin's confidence is in, and
  is not being changed here.
- Claims already sitting at the legacy `invoice_request_drafted` flag when
  this ships are unaffected and keep going through the manual draft-and-send
  path (`reconcile_sent_invoice_requests`, the dashboard's "Invoice-request
  sent" button) — both already scope narrowly enough (`draft_id IS NOT NULL`)
  that they never match a claim created by the new path, so nothing needed
  to change there to keep them working.
- Any future request to auto-send a *different* email type needs its own
  explicit decision from Justin — this ADR's scope is not a precedent for
  "sending is fine now," it is a named, narrow exception.
