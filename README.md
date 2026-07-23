# OpenClaw

Personal assistant that watches for vet expenses and does the insurance-claim legwork: detect the bank charge, find the matching invoice in Gmail, fill the insurer's PDF claim form, stage a ready-to-send Gmail draft, then track the insurer's replies (acknowledgement → info requests/suspensions → settlement) until the money lands. Also captures tasks/reminders from email onto a local dashboard.

Built for one household (two dogs, two insurers). Three promises it never breaks:

- **Never sends email.** Gmail drafts only — Justin reviews and hits send himself.
- **Never stores bank logins.** Transactions arrive via manual NetBank CSV upload.
- **Never guesses required claim fields.** Anything it can't derive from a document (the claimed condition, an ambiguous pet) is flagged and asked — on Telegram and the dashboard — not inferred.

## The goal

Vet visits generate a paper chase: a card charge, an emailed invoice (sometimes weeks later, sometimes forwarded by a spouse, sometimes a photo scan), a 4-row insurer claim form, and a reply thread that decides whether money comes back. OpenClaw's job is to make Justin's part of that chase three taps: pick a condition, review a draft, hit send. Everything mechanical — finding, matching, extracting, filling, tracking — is automated, and everything the software isn't *sure* about is surfaced as an explicit question rather than a silent guess.

## The process, end to end

```
NetBank CSV upload (dashboard)
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
        │       amounts out of the PDF attachment
        ▼
Telegram + dashboard ── every state change, question and blocker lands as a
                        message with the claim #id and one-tap buttons
```

The whole pipeline runs on an APScheduler tick (default 15 min) inside one FastAPI process (ADR-0006). A failure on one claim flags that claim and moves on — a tick is never lost to one bad email (visible failures are a hard rule).

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
   - Pet assignment is read off printed facts only — the email naming exactly one known pet, or the invoice's patient field. Both dogs named / nothing printed → Telegram asks.
5. **Claim math**: the form never claims the bank charge. It claims the invoice's **claimable subtotal** — line items minus routine-care keywords (vaccination, worming, flea, …) — against the charge as a ceiling.

## Third-party calls (complete list)

| Service | What for | What's sent | Auth |
|---|---|---|---|
| **Gmail API** (Google) | Search/read mail + attachments; create/update **drafts**; never `send()` | Search queries (merchant names, dates), message/attachment reads; drafts containing filled claim PDFs | OAuth token in `app/data/token.json` (testing-app 7-day expiry; re-auth: `python scripts/gmail_auth.py`) |
| **Groq** (default LLM, `llama-3.3-70b-versatile`, free tier) | Invoice text extraction; Telegram free-chat agent | Email/PDF text of candidate invoice emails; chat prompts | `GROQ_API_KEY` |
| **Gemini** (Google, `gemini-2.5-flash`, free tier) | **Vision OCR** of scanned invoice PDFs (always, regardless of provider); full text-LLM rollback if `LLM_PROVIDER=gemini` | Downscaled JPEG of scan pages | `GEMINI_API_KEY` |
| **OpenAI** (optional, `gpt-4o-mini`) | Paid fallback provider — only if `LLM_PROVIDER=openai` | Same as Groq | `OPENAI_API_KEY` |
| **Telegram Bot API** | Notifications, questions with tap-buttons, document (PDF) review messages, 👍 receipt acks, free-chat queries | Claim summaries (amounts, dates, vet names, pet names), invoice PDFs for review | `TELEGRAM_BOT_TOKEN`; single authorized username |
| **Google Drive** (via `db_backup`) | SQLite DB backup | The database file | Same Google OAuth |

Every LLM call is rate-limited and logged to the `llm_calls` table (provider, purpose, latency, error). No other network calls exist; the bank is never contacted.

## Storage

- `app/data/openclaw.db` — SQLite: transactions, claims, status events, extraction cache, vision attempt counts, split proposals, tasks/reminders.
- `/data/claims` (container) = `app/data/claims` — filled claim-form PDFs.
- `/data/invoices` = `app/data/invoices` — per-visit invoice PDFs sliced out of vet emails.
- `app/data/` also holds Gmail credentials/token. The whole directory and `app/.env` are gitignored.

## Setup

```
cd app
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env        # fill in: Groq + Gemini keys, owner/policy details, bank payout details, Telegram token
python scripts/gmail_auth.py   # one-time OAuth consent (opens a browser)
.venv/Scripts/uvicorn openclaw.main:app --port 8000
```

Dashboard at `http://localhost:8000` — upload a NetBank CSV there to kick the pipeline. Production runs in Docker (`docker compose up -d --build`) with `app/data` bind-mounted at `/data`.

Tests: `cd app && .venv/Scripts/python tests/test_core.py` — assert-based, no pytest, fully hermetic (all LLM keys force-blanked, vision calls stubbed; tests never spend API tokens).

## Docs

- `CLAUDE.md` (root) — hard rules + hard-won domain knowledge for AI-assisted sessions; `app/openclaw/CLAUDE.md` — module map.
- `docs/adr/` — architecture decisions. Start with 0006 (service boundary), 0007 (ceiling matching), 0008 (status event log), 0009 (LLM backends), 0010 (vision OCR).
- `docs/prd/` — original product requirements.
- `openspec/changes/` — spec-driven change history; each change's `tasks.md` records what was verified against real data.
