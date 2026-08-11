# OpenClaw

Personal assistant that watches for vet expenses and does the insurance-claim legwork: detect the bank charge, find the matching invoice in Gmail, fill the insurer's PDF claim form, stage a ready-to-send Gmail draft, then track the insurer's replies (acknowledgement → info requests/suspensions → settlement) until the money lands. Also captures tasks/reminders from email onto a local dashboard.

Built for one household (two dogs, two insurers). Three promises it never breaks:

- **Never sends email.** Gmail drafts only — Justin reviews and hits send himself.
- **Never stores bank logins.** Transactions arrive via manual NetBank CSV upload — the dashboard, or a document sent to the Telegram bot (`/upload-tx` to import a just-sent file; a caption-less document is also picked up directly when the gateway's dispatch hooks fire).
- **Never guesses required claim fields.** Anything it can't derive from a document (the claimed condition, an ambiguous pet) is flagged and asked — on Telegram and the dashboard — not inferred.

## The goal

Vet visits generate a paper chase: a card charge, an emailed invoice (sometimes weeks later, sometimes forwarded by a spouse, sometimes a photo scan), a 4-row insurer claim form, and a reply thread that decides whether money comes back. OpenClaw's job is to make Justin's part of that chase three taps: pick a condition, review a draft, hit send. Everything mechanical — finding, matching, extracting, filling, tracking — is automated, and everything the software isn't *sure* about is surfaced as an explicit question rather than a silent guess.

## The process, end to end

```
NetBank CSV upload (dashboard, or Telegram document + /upload-tx)
        │
        ▼
vet_detection ── keyword match on merchant, LLM fallback, non-vet denylist
        │              creates a vet_claims row per vet charge
        ▼
invoice_matching ── Gmail search per claim (see "How matching works")
        │   ├─ matched → invoice + amount + date recorded on the claim
        │   ├─ nothing found → drafts an invoice-request email TO the vet (Justin sends)
        │   ├─ scan unreadable → vision-OCR fallback (3 attempts max) → else flag
        │   └─ invoice bigger than the charge → split/merge proposal on Telegram
        ▼
claim_forms ── slice the claim's own invoice pages into /data/invoices,
        │      fill the Petcover PDF form, batch up to 4 same-pet claims,
        │      create ONE Gmail draft (form + invoices attached)
        ▼
Justin sends the draft (manually, always)
        ▼
claim_status ── poll Petcover replies: learn their claim reference from the
        │       acknowledgement, log every event append-only, parse settlement
        │       amounts out of the PDF attachment, and run TWO independent
        │       settlement checks (their arithmetic; what they assessed).
        │       An information request also
        │       records WHO owes the document (To:/Cc: vs vet_contacts) and WHAT
        │       it is ("Consultation notes dated 18/05/2026"), and resolves that
        │       date to the visit we already hold — usually a different claim's.
        ▼
Telegram + dashboard ── every state change, question and blocker lands as a
                        message with the claim #id and one-tap buttons
```

The whole pipeline runs on a 15-minute tick inside one FastAPI process (ADR-0006). The tick is driven by the **gateway's cron** since 2026-08-04 (`POST /internal/tick`); APScheduler is still in the code behind `SCHEDULER_ENABLED` for one week's rollback, and off in the deployed config. A failure on one claim flags that claim and moves on — a tick is never lost to one bad email (visible failures are a hard rule).

**Settlement checking asks two separate questions**, because they have different answers and
different audiences. An approval letter states its own full breakdown — amount claimed, fixed excess,
non-claimable amount, age contribution (a rate and a dollar figure), percentage excess, amount paid —
so *Check A* re-adds those figures and confirms they reach the amount Petcover says they paid. That is
arithmetic on numbers they printed, not a model of their policy: nothing is inferred, and a letter
stating no rate is skipped rather than assumed. *Check B* compares the amount they say they assessed
against the claimable subtotal we actually submitted, and asks about a difference rather than
asserting one — every stated amount matches some real invoice of ours, the amounts cross Condition
Thread boundaries, and the letters carry no invoice number, so re-routing a settlement is Justin's
call after Petcover answers. Across all ten live approval letters (2026-08-04) Check A passes to the
cent and Check B has questions about five of them. The claim amount used is always the recorded
claimable subtotal, never the invoice total and never the bank charge: one accessor reads it, a guard
test fails if any caller re-adds the old fallback, and where it was never recorded the surfaces say
`Not recorded` instead of a plausible substitute.

**Check B and unrecorded-subtotal flags have a resolution path, not just a flag.** Every claim
carrying an open Check B (assessment-difference) or unrecorded-claimable-subtotal flag surfaces a
settlement-review card on the dashboard — condition, submitted vs. assessed figures, and the invoice's
line items or a PDF link — with two actions. **Acceptable** dismisses it terminally (reuses the same
manual-dismiss mechanism as today; never rewrites `claimable_subtotal` or a paid amount). **More Info**
queues the claim into a single open, consolidated Gmail draft to Petcover asking them to confirm what
they assessed (draft only — Justin reviews and sends it himself); the claim then reads "waiting on
Petcover", not "needs your action". When Petcover replies on that thread, the reply is correlated by
Gmail thread id (not the usual reference/Sr/pet-condition router) and an LLM extracts `{claim
identifier, confirmed amount}` pairs — the ONE new LLM call site this adds, everywhere else stays
regex/keyword. A pair that matches a claim's recorded claimable subtotal to the cent resolves it
exactly as Acceptable would; anything else — no match, an unaddressed claim, a different figure —
leaves the claim waiting and resurfaces the same card with the reply's figure shown, never guessed.

Petcover's own mail is also excluded from the assistant's task capture. Both pollers share
`processed_emails` as their "seen it" gate, so whichever ran first won — which is how five approval
letters became to-do items and reached no claim at all.

The lifecycle above is a **declared state machine**, not a column anyone may write. Every legal move is in one transition table, and `claim_status.apply_event` is the only thing that writes a claim's state: it records the event first, then applies it if the table allows the move — and if it doesn't, the state stays put and the claim is flagged naming both states, with the event kept as evidence. So a claim's history is a fact on record rather than something to reconstruct: re-reading an old acknowledgement can no longer walk a settled claim backwards, which it did to two claims in July 2026 (two others moved the same day by being routed to the wrong claim, which is a separate guard). Each tick folds every claim's events and compares the result against the stored status; `/health` publishes the disagreement count, and it should read zero. Reverting a state change, and the timeline view that would show it, are not built yet.

## How matching works

For each unmatched vet charge, `invoice_matching`:

1. **Searches Gmail** with layered queries: merchant-name search in a narrow window (charge date ±3 days) plus an unconditional open-ended one (invoices arrive months late — confirmed live), and the same pair for mail forwarded by the spouse's address. Own outgoing mail is excluded twice (`-from:me` in the query, SENT-label check on results) — the system's own invoice-request emails once false-matched 12 claims.
2. **Extracts invoices once per email** with the LLM (multi-invoice JSON: date, total, line items, patient). The parsed result is cached forever in `email_extractions`; a failed parse is not cached so it retries. If the PDF is an image-only scan (no text layer), the **vision-OCR fallback** reads it page-by-page with Gemini — hard-capped at 3 attempts per email, attempts refunded on provider outages, success cached like any extraction (ADR-0010).
3. **Gates every candidate invoice** (ADR-0007):
   - **Ceiling**: invoice total ≤ bank charge (+1c). Card surcharges make charges run *over* the invoice; an invoice larger than the charge can't be the one this charge paid.
   - **Date plausibility**: the invoice's own service date must sit near the transaction date (arrival date is only a search hint).
   - **Not already claimed**: an invoice another claim carries is off the table (identity: invoice number, else amount+date) — bulk bundles surface many small invoices that would otherwise slip under a bigger charge's ceiling.
   - **Best fit wins**: remaining candidates rank by closest amount, then closest date.
4. **Handles the special cases** instead of guessing:
   - Invoice **exceeds** the charge but the date fits → probably one invoice paid over several card swipes. If a sibling charge completes the sum, a **merge proposal** goes to Telegram (with the invoice PDF attached) — Justin confirms; the larger charge carries the invoice, the other closes as its second payment.
   - Charge with no invoice anywhere → an **invoice-request email to the vet** is drafted (never sent) using the visit date and amount.
   - **Petcover asks for a missing document** → the claim reads as what it needs ("Vet: consult notes needed"), naming the party who owes it, and a **Monday-morning nudge** lists every vet request nobody has answered with the clinic's address, the document, the invoice the requested date belongs to, and the days left against the one-year deadline — counted from the date the pet was **treated**, which is not the date of the charge (real: treated 19 and 30 June 2026, both charged 6 July, so counting from the charge would have granted 17 and 6 days the policy doesn't give). Taken from the earliest date the invoice states; with no invoice on file it falls back to the charge date and says the anchor was assumed. A vet's reply goes to Petcover, never to us, so "unanswered" can only mean the claim still sits on an unresolved request. No chase email is drafted — Justin chases; the system makes the chase answerable.
   - Pet assignment is read off printed facts only — the email naming exactly one known pet, or the invoice's patient field. Both dogs named / nothing printed → Telegram asks.
   - **One charge, two invoices** (one card payment settling both dogs' bills — confirmed live: $407.56 = Aari $35.00 + Echo $369.33 + $3.23 surcharge) → the matcher apportions it itself: this claim takes one invoice, a second claim on the same charge takes the other, each with its own invoice, pet and claimable subtotal. Only when exactly one candidate closes the charge; two possibilities means it refuses rather than guesses. ADR-0019.
   - **Receipts paid later than the visit** are matched on their own payment line (`06/07/2026 Credit Card $35.00`), not the ±3-day service-date window — the visits above were 19 and 30 June.
   - **One invoice, two pets** (a single document billing both dogs) → reply to the pet card in Telegram with the pets and one pet's share; a Confirm tap turns it into **one claim per pet**, each carrying its own share of the claimable subtotal. Shares come from Justin — only the last is derived as the remainder. A pet whose insurer has no defined process (Echo/Bow Wow) still gets a claim; it sits blocked and visible instead of being lost. ADR-0019.
5. **Claim math**: the form never claims the bank charge. It claims the invoice's **claimable subtotal** — line items minus routine-care keywords (vaccination, worming, flea, …) — against the charge as a ceiling. On a per-pet split, each claim carries only its own share and the charge is the ceiling on their sum.

## Third-party calls (complete list)

| Service | What for | What's sent | Auth |
|---|---|---|---|
| **Gmail API** (Google) | Search/read mail + attachments; create/update **drafts**; never `send()` | Search queries (merchant names, dates), message/attachment reads; drafts containing filled claim PDFs | OAuth token in `app/data/token.json` (testing-app 7-day expiry; re-auth: `python scripts/gmail_auth.py`) |
| **Gemini** (Google, free tier) — **the default LLM since 2026-08-04** | Invoice text extraction; vision OCR of scanned invoice PDFs (always, regardless of provider); the gateway agent's chat turns (`gemini/gemini-2.5-flash`) | Email/PDF text of candidate invoice emails; downscaled JPEGs of scan pages; chat prompts | `GEMINI_API_KEY` |
| ↳ four Gemini models are called, not one | `gemini-2.5-flash` is the primary; on daily exhaustion the chain falls through to `gemini-3.6-flash` → `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`, each with its own budget, and the reply says which one answered (ADR-0017). Text extraction goes through Gemini's own SDK; the chat tool loop goes through Google's OpenAI-compatible surface at `/v1beta/openai` | Same as above | Same key |
| **Groq** (configured, currently unreachable) | Was the default LLM. This network is **blocked by Groq**: a request with no auth header at all gets `403 "Access denied. Please check your network settings."` Left configured so a network that can reach it costs one line — ADR-0009's 2026-08-04 amendment | Same as Gemini's text calls | `GROQ_API_KEY` |
| **OpenAI** (optional, `gpt-4o-mini`) | Paid fallback provider — only if `LLM_PROVIDER=openai` | Same as Gemini's text calls | `OPENAI_API_KEY` |
| **Telegram Bot API** | Notifications, questions with tap-buttons, document (PDF) review messages, 👍 receipt acks, free-chat queries | Claim summaries (amounts, dates, vet names, pet names), invoice PDFs for review | `TELEGRAM_BOT_TOKEN`; single authorized username |
| **Google Drive** (via `db_backup`) | SQLite DB backup | The database file | Same Google OAuth |
| **OpenClaw gateway** *(deployed 2026-08-02; not yet polling — see "In flight" below)* | Will own Telegram transport, the chat agent loop, model routing and cron | Same claim summaries the Bot API already carries; **no Gmail credential and no database access, deliberately** | Runs locally in Docker; holds no `TELEGRAM_BOT_TOKEN` until the cutover |

Every LLM call is rate-limited and logged to the `llm_calls` table (provider, purpose, latency, error). No other network calls exist; the bank is never contacted.

## Storage

- `app/data/openclaw.db` — SQLite: transactions, claims, status events, extraction cache, vision attempt counts, split proposals, tasks/reminders, and every Telegram message in and out (`telegram_messages` — audit trail, replay queue and training data in one table, ADR-0014). Runs in **WAL** mode with a 5s busy timeout: the host and the container both open this bind-mounted file, and the default rollback journal produced a `disk I/O error` when they overlapped. **Query it from the host read-only** — `sqlite3.connect("file:data/openclaw.db?mode=ro", uri=True)`. A read-write open (the default, even for pure `SELECT`s) checkpoints and deletes the WAL sidecars on close, which left the container unable to open the DB at all for 51 minutes on 2026-07-25. ADR-0018.
- `/data/claims` (container) = `app/data/claims` — filled claim-form PDFs.
- `/data/invoices` = `app/data/invoices` — per-visit invoice PDFs sliced out of vet emails.
- `app/data/` also holds Gmail credentials/token. The whole directory and `app/.env` are gitignored.

## Setup

```
cd app
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env        # fill in: Gemini key (required), Groq key (optional), owner/policy details, bank payout details, Telegram token
python scripts/gmail_auth.py   # one-time OAuth consent (opens a browser)
.venv/Scripts/uvicorn openclaw.main:app --port 8000
```

Dashboard at `http://localhost:8000` — upload a NetBank CSV there to kick the pipeline. Also: `/basic` (phone-first card view), `/health` (deploy version, whether Telegram polling is actually alive, unprocessed-message count), `/messages.jsonl` (the full Telegram message stream for reinforcement learning, one JSON object per line).

Production runs in Docker via `./scripts/deploy.ps1`, which stamps the image with the git SHA and prints `/health` afterwards; `app/data` is bind-mounted at `/data`. A bare `docker compose up -d --build` works but leaves the deploy version `unknown`, which mistags every logged message (ADR-0015).

Tests: `cd app && .venv/Scripts/python tests/test_core.py` — assert-based, no pytest, fully hermetic (all LLM keys force-blanked, vision calls stubbed; tests never spend API tokens).

## Deploying

Two runtimes as of the gateway swap — see **[docs/gateway-deploy.md](docs/gateway-deploy.md)**
for the command, the two `.env` files and, most importantly, the list of things
the preflight *cannot* assert.

## In flight: the OpenClaw gateway swap

**Slice 1 is deployed** (2026-08-02). Both runtimes run; the app still owns Telegram exactly as documented above, and the gateway holds no bot token. Filling that token in is the cutover, and it has not happened.

`openspec/changes/openclaw-gateway-core` replaces the hand-rolled transport, chat loop and scheduler with **OpenClaw the product** (an unrelated local-first gateway daemon; the repo has shared its name since inception and never depended on it). After the swap there are two runtimes: the gateway owns the Telegram token, polling, the agent loop, model routing and cron; this app keeps claims, Gmail, SQLite and the dashboard, reached over a secret-guarded `/internal` surface and an enumerated MCP tool inventory.

**Live now:** a second compose service running the gateway, `internal_api.py` (`/internal/*`,
secret-guarded), `mcp_server.py` (the claims read surface at `/mcp`), `media_outbox.py`,
`app/gateway-plugin/` (registers the app's five slash commands inside the gateway) and
`app/gateway-workspace/` (the agent's prompt files, injected every turn). `gateway_client.py` is
merged and has no caller until the cutover.

**Cut over:** Telegram polling and the chat agent (the gateway holds the token; the app's updater
is off), and scheduling — five cron entries in the gateway drive `/internal/tick`, `/internal/ingest`,
`/internal/nudge`, `/internal/vet-nudge` and `/internal/expire-queue`. The calendar jobs run at 09:00 **Australia/Sydney** — under APScheduler they ran in the container's UTC, so a morning nudge arrived that evening. Reminders are the one job cron
cannot express (a one-shot at an arbitrary minute), so they sweep on the tick.

**Unchanged:** everything the sections above describe. See [docs/gateway-deploy.md](docs/gateway-deploy.md) — especially the list of what the
deploy preflight *cannot* assert.

Read ADR-0024 (why the domain is not ported), ADR-0025 (where the proposal gate lives) and ADR-0023 (why the agent's tool allowlist is load-bearing for both security and cost) before touching any of it.

## Docs

- `CLAUDE.md` (root) — hard rules + hard-won domain knowledge for AI-assisted sessions; `app/openclaw/CLAUDE.md` — module map.
- `docs/adr/` — architecture decisions. Start with 0006 (service boundary), 0007 (ceiling matching), 0008 (status event log), 0009 (LLM backends), 0010 (vision OCR). For the Telegram side: 0014 (durable message log + replay queue) and 0015 (restart on a dead updater; what ERROR means). For the gateway swap: 0023 (tool allowlist), 0024 (gateway as shell), 0025 (proposal gate). Several older ADRs carry dated addenda from the swap rather than edits — 0002, 0003, 0009, 0014, 0015, 0017.
- `docs/prd/` — original product requirements.
- `openspec/changes/` — spec-driven change history; each change's `tasks.md` records what was verified against real data.
