## Context

OpenClaw already runs locally (FastAPI/APScheduler/SQLite/Gemini, see ADR-0001/0002) with read-only Gmail polling (ADR-0004). This change bolts on a money-touching pipeline: card transaction → vet detection → invoice match → claim form fill → draft claim email. Justin banks with Commbank (Australia), so bank access options are constrained by Australian regulation (Consumer Data Right / CDR) and Commbank's ToS (no credential-scraping).

## Goals / Non-Goals

**Goals:**
- Detect vet-related Commbank credit card transactions automatically.
- Match a detected transaction to its invoice email.
- Auto-fill Justin's real pet-insurance claim template with transaction + invoice data.
- Produce a ready-to-send claim email that Justin reviews before it goes out.

**Non-Goals:**
- Fully autonomous sending of the claim email without human review (money + external party = review gate stays, at least for v1).
- Notion integration — explicitly deferred, not designed here.
- Multi-bank support — Commbank only for v1.
- General bank-transaction budgeting/categorization beyond "is this a vet payment".

## Decisions

### Decision: Bank transaction access via manual CSV export from NetBank — NOT YET CONFIRMED BY JUSTIN
Justin asked "is there a service we can use to monitor credit card transactions, even 3rd party" and confirmed he wants a genuinely free option and doesn't care who connects to the account underneath. Every option checked turned out to fail the free-and-automated combination:

- **Basiq** — pricing page lists $0.50/user/month for the data product, but that's on top of an unquantified "platform access fee," a 12-month minimum commitment, and sales-gated enrollment (no self-serve signup). Real all-in monthly cost for one personal account is unknown and could be much higher than $0.50 once the platform fee is included. No free live-data tier either way.
- **Fiskil** — pricing sales-gated, only a sandbox is free.
- **illion Open Data** — enterprise-grade, same expected problem.
- **PocketSmith** — free-plan page lists "automatic bank feeds" as included, but Justin hit a paywall/feed-credit charge trying to actually connect Commbank in practice.
- **YNAB** — no permanent free tier, no native AU bank feed either.
- **Commbank's own Transaction Notifications** — genuinely free, but delivery is app push or SMS only. **No email option exists**, which kills the "parse via existing Gmail poller" plan entirely — checked directly against Commbank's setup instructions, not assumed.

With every automated free path exhausted, the only option confirmed genuinely free with zero new dependencies is **manual CSV export from NetBank**, uploaded by Justin into OpenClaw. This is a real reduction in scope from the original "fully automatic detection" goal — flagged clearly, not smoothed over. **Needs Justin's explicit sign-off**, since this trades away the "notice the payment without being told" part of the goal in exchange for staying free and simple.

**Alternatives considered:**
- **Screen-scraping Commbank online banking** (e.g. via Playwright) — Pros: no third party, no signup. Cons: against Commbank's ToS, breaks on any UI change, stores banking credentials somewhere. **Why not**: ToS violation + credential risk, ruled out immediately, never seriously in the running.
- **Direct CDR aggregator (Basiq/Fiskil/illion)** — Pros: structured API, real-time. Cons: none had a genuinely free live-data tier once actually checked. **Why not**: fails the explicit free-tier requirement.
- **PocketSmith** — Pros: structured API, dashboard/budgeting features as a bonus. Cons: free plan's "automatic bank feeds" gated behind a paid feed credit in practice. **Why not**: doesn't actually satisfy "free" once tested against the real signup flow.
- **Commbank Transaction Notifications parsed via Gmail** — Pros: zero new dependency, uses existing Gmail poller. Cons: no email delivery option exists, only app push/SMS. **Why not**: technically impossible as designed — not a trade-off, a dead end.
- **Frollo (free CDR-based app)** — Pros: genuinely free consumer app, no paid gate on connecting Commbank, has a built-in CSV export emailed to a verified address (would slot into the existing Gmail poller with zero new credential risk). Cons: export is manual-trigger only inside the app; the Frollo developer API (which could otherwise automate the trigger) is confirmed business/partner-only — requires contacting Frollo directly for a Client ID/Secret, not self-serve for an individual with a personal account. **Why not fully automated**: no legitimate way found for OpenClaw itself to trigger the export on a schedule without either a manual tap or UI automation.
- **SMS-to-somewhere via phone automation (e.g. MacroDroid)** — Pros: stays free, recovers automatic detection. Cons: adds a phone-side moving part (another app, another failure point) that OpenClaw's dashboard-only/local-only design didn't otherwise need. **Why not**: not ruled out, just not the default — worth a fast-follow if manual CSV proves too tedious in practice.
- **Playwright automation of just the Frollo export-button click** (not full bank scraping) — Pros: real automation, dramatically lower blast radius than automating a bank login since Frollo is read-only under CDR (no payment/transfer capability) and the consent is revocable independent of any scraping. Cons: still requires storing a Frollo session/credential unattended, likely still against Frollo's consumer ToS, still brittle to UI changes. **Why not (for now)**: meaningfully different risk profile than bank-login automation, but still a real trade-off Justin needs to explicitly accept, not a default to build silently.
- **Manual CSV export/import** — Pros: zero integration risk, no third party at all, definitely free, definitely works. Cons: fully manual, defeats the "automatic" half of the original goal. **Why not**: not rejected — this is the current pick, with the trade-off named explicitly rather than hidden.
- **Plaid** — Pros: well-known, great docs. Cons: Plaid's Australian bank coverage is limited/exists mainly via partners; not CDR-native for AU. **Why not**: worse AU fit than the alternatives above even before the cost comparison.
- **YNAB** — Pros: well-known budgeting app with a documented free API. Cons: no permanent free tier itself ($109/yr), and no native AU bank feed — still needs a paid third-party CDR bridge underneath. **Why not**: strictly worse than the alternatives above on both axes Justin cares about (free, and actually connects to Commbank).

### Decision: Vet detection is a two-stage filter, not one Gemini call per transaction
Stage 1: cheap local heuristic (merchant name/category code contains vet-like keywords, or matches a small user-maintained allowlist of known vet merchant names). Stage 2: only ambiguous cases (no keyword hit but category is "medical"/"pet") go to Gemini for a judgement call. Keeps Gemini calls (rate-limited, see ADR-0001) off the hot path for the common case.

**Alternatives considered:**
- **Gemini on every transaction** — Pros: simplest code. Cons: burns the 15rpm free-tier budget on transactions that are obviously groceries/fuel/etc. **Why not**: wasteful, unnecessary API dependency for the easy 95% of cases.

### Decision: Invoice matching by vendor name + amount + date window, confirmed via Gemini extraction of the email body
After a vet transaction is detected, search Gmail (reusing the existing read-only integration) for messages from/about that vendor within ±3 days of the transaction date, then use Gemini to extract structured invoice fields (date, amount, itemized services) from the matching email/attachment text. If amount doesn't match transaction amount within a small tolerance, flag as unmatched rather than guessing.

**Alternatives considered:**
- **Attachment-only matching (PDF parsing, ignore email body)** — Pros: invoices are often PDFs, more structured. Cons: some vets email details in-body with no attachment. **Why not**: narrower coverage; PDF parsing can be added later as an enhancement, not a blocker for v1.

### Decision: Claim template filled via docxtpl (Word template) or pypdf form-fill (if insurer's template is a fillable PDF) — decided per Justin's actual template once supplied
Can't finalize this without the real file. Both are lightweight, no new heavy dependency, and match the "no autonomous send" goal — the filled file becomes an attachment on the draft email.

**Alternatives considered:**
- **OCR + generic text overlay on a flat (non-fillable) PDF** — Pros: works on any template no matter how it's made. Cons: fragile coordinate-based text placement, breaks if insurer changes template layout. **Why not**: only fall back to this if the real template turns out to be a flat scan with no form fields.

### Decision: Gmail scope widened to include `gmail.send`, but OpenClaw only ever creates drafts, never calls `send` for the claim email in v1
Even with the wider scope granted, the app-level code path only drafts (`users.drafts.create`); Justin sends manually from the dashboard link or his own Gmail. This keeps the actual "money leaves the house" action human-gated while still needing the broader OAuth consent (draft creation requires it too).

**Alternatives considered:**
- **Stay read-only, tell Justin to copy-paste the filled form into a new email himself** — Pros: no scope change at all. Cons: reintroduces the manual step the whole feature exists to remove. **Why not**: defeats the purpose; drafting (not sending) is the right compromise.

### Decision: New tables `bank_transactions` and `vet_claims` in the existing OpenClaw SQLite DB
Mirrors the existing schema style (`tasks`, `reminders`, `llm_calls`, `processed_emails` — see ADR-0002). `bank_transactions` dedupes by aggregator transaction ID; `vet_claims` links a transaction to its matched invoice email and claim status (`pending_match`, `matched`, `drafted`, `submitted_by_user`).

## Risks / Trade-offs

- **[Risk] Manual CSV upload means detection isn't actually automatic** → the core "notice the payment without being told" goal is only partly met; Justin still has to remember to export/upload. Mitigation: if this proves too manual in practice, fast-follow with SMS-forwarding automation (MacroDroid) or reconsider a paid aggregator.
- **[Risk] False-positive vet detection drafts a claim for an unrelated transaction** → Mitigation: draft-only (never auto-send) + dashboard shows the matched transaction/invoice side-by-side for Justin to confirm before sending.
- **[Risk] Invoice email never arrives or doesn't match within the date window** → Mitigation: transaction sits in `pending_match` status and surfaces on the dashboard as a manual-follow-up item (reuses existing reminder/task surfacing pattern).
- **[Risk] Storing bank transaction data locally is a bigger blast radius than task/email metadata if the SQLite file is ever exposed** → Mitigation: no bank credentials stored anywhere in this design at all (nothing but Commbank's own alert emails involved); only transaction metadata (date/amount/merchant) lives in OpenClaw's DB, consistent with the existing local-only/no-exposed-port posture.
- **[Risk] Widening Gmail scope to include send/drafts increases what a compromised local process could do** → Mitigation: scope is still not full `mail.google.com`; drafts-only usage is enforced at the application code level even though the granted OAuth scope is broader.

## Open Questions

- Which pet insurer, and is their claim template a fillable PDF or a Word doc? (blocks the exact fill-library choice)
- Does Justin actually want the manual-CSV approach, given it drops automatic detection, or would he rather accept the phone-automation route or a small paid aggregator cost to keep it automatic?
- How often would Justin realistically remember to export/upload a CSV — daily, weekly? Affects how stale `pending_match` follow-ups can get.
- Insurer's claim-submission email address / portal — confirm claims go by email at all, some insurers require a portal upload instead.
