# OpenClaw

Personal assistant that watches for vet expenses and does the insurance-claim legwork: detect the bank charge, find the matching invoice in Gmail, fill the insurer's PDF claim form, stage a ready-to-send Gmail draft, then track the insurer's replies (acknowledgement → info requests/suspensions → settlement) until the money lands. Also captures tasks/reminders from email onto a local dashboard.

Built for one household (two dogs, two insurers). It **never sends email** (drafts only), **never stores bank logins** (manual CSV upload), and **never guesses** required claim fields — anything it can't derive is flagged for a human.

## How it works

```
NetBank CSV upload ─→ vet_detection ─→ invoice_matching ─→ claim_forms ─→ Gmail draft
                      (keyword +        (Gmail search +      (fill Petcover      │ you send
                       Gemini fallback)  Gemini line-item     PDF, batch up      ▼
                                         extraction)          to 4 invoices)   claim_status
                                                                              (poll Petcover
                                                                               replies, event log,
                                                                               dashboard alerts)
```

- **Claims service** (`app/openclaw/`): `vet_detection`, `invoice_matching`, `claim_forms`, `claim_status`, orchestrated by `pipeline` on an APScheduler interval. One FastAPI app, SQLite storage (ADR-0006).
- **Money rule**: the bank charge is the *ceiling*; the claim carries the invoice's *claimable subtotal* — routine care (vaccination, worming…) excluded (ADR-0007).
- **Status tracking**: append-only event history per claim; open info requests stay on the dashboard until explicitly confirmed resolved (ADR-0008).

## Setup

```
cd app
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env        # fill in: Gemini key, owner/policy details, bank details
python scripts/gmail_auth.py   # one-time OAuth consent (opens a browser)
.venv/Scripts/uvicorn openclaw.main:app --port 8000
```

Dashboard at `http://localhost:8000`. Upload a NetBank CSV there to kick the pipeline.

Tests: `cd app && .venv/Scripts/python tests/test_core.py`

## Docs

- `docs/adr/` — architecture decisions (stack, Gmail polling, claims-service boundary, ceiling matching, event-log tracking)
- `docs/prd/` — original product requirements
- `openspec/changes/` — spec-driven change history (proposal/design/specs/tasks per change)
- `CLAUDE.md` — working rules and hard-won domain knowledge for AI-assisted sessions

## Secrets

`app/.env`, `app/data/` (SQLite DB, Gmail token/credentials) are gitignored. Nothing sensitive belongs in the repo.
