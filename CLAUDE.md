# OpenClaw

Personal assistant for Justin: task/reminder capture from Gmail, plus a vet-insurance claims service that turns bank transactions into ready-to-send Petcover claim drafts and tracks their lifecycle. Single user, runs locally.

## Layout

- `app/openclaw/` — the FastAPI app. Claims service modules: `vet_detection`, `invoice_matching`, `claim_forms`, `claim_status`, orchestrated by `pipeline` (see ADR-0006 — logical boundary, never a separate deployable). Assistant side: `tasks`, `reminders`, `gmail_ingest`.
- `app/tests/test_core.py` — assert-based smoke suite, run with `./.venv/Scripts/python.exe tests/test_core.py` from `app/`. No pytest.
- `openspec/changes/` — spec-driven change workflow (proposal → design → specs → tasks; `/opsx:propose`, `/opsx:apply`).
- `docs/adr/` — architecture decisions; read 0006–0008 before touching the claims service.
- `app/data/` and `app/.env` — real SQLite DB, Gmail credentials/token, secrets. Gitignored; never commit, never print contents.

## Hard rules (non-negotiable)

- **Never send email.** Gmail drafts only — `drafts().create`/`update`, never `send()`. Justin reviews and sends himself.
- **Never guess required claim fields.** `condition_text` and anything else Justin must supply gets flagged on the dashboard, not inferred.
- **Never store bank login credentials.** Transactions arrive via manual NetBank CSV upload only.
- **Failures are visible.** Follow the existing pattern: write a human-readable reason to `vet_claims.flag` / surface on the dashboard. No silent no-ops, no swallowed exceptions.

## Domain rules that were hard-won (don't re-derive)

- Bank charge = **ceiling** on a claim, not an equality target (card surcharge, multi-invoice charges). Claim form carries the **claimable subtotal** (line items minus `NON_CLAIMABLE_KEYWORDS`). ADR-0007.
- Claim status = append-only `claim_status_events`; needs-action persists until Justin's explicit confirm-resolved click. `unclassified` events never write claim status. ADR-0008.
- A batch submission = up to 4 invoices, one draft, one Petcover reference; claims sharing a `draft_id` move together (mark-sent, correlation, learned reference).
- Petcover's claim reference is learned from their acknowledgement reply (formats changed over the years: `GABR-####`, `ELD-##-####`, `DC1-##-####`); it is NOT the policy number. Extract via context phrases only ("Claim Number …"), never bare patterns.
- Settlement dollar breakdowns exist only in the PDF attachment, not the email body — `gmail_client.full_message_text` includes PDF text for this reason.
- Correlation fallback is pet-name + submitted-status pool; **no date windows** — a claim's transaction can be a year older than its submission.

## Operational constraints

- LLM backend is provider-agnostic (`llm.py`, `chat()`/`extract()`); default is Groq free tier (`llama-3.3-70b-versatile`), swappable to Cerebras/OpenAI/Gemini by env var — ADR-0009 (supersedes 0001). Cerebras' free tier is sold-out for this account (402 on every model), hence Groq. Chat + extraction are the only LLM users. Don't add LLM calls where regex/keywords work (classification, references are keyword/regex on purpose).
- Gmail OAuth token expires periodically (testing-app 7-day limit) — recovery: `python scripts/gmail_auth.py` (opens browser, Justin must click Allow).
- Live DB schema changes need a manual `ALTER TABLE` against `app/data/openclaw.db` — `CREATE TABLE IF NOT EXISTS` in `db.py` won't touch existing tables.

## Working style

- Verify against real data before declaring anything correct or broken — this project's history is a string of plausible assumptions broken by real emails/PDFs/CSVs. Test hypotheses on the real DB/Gmail (read-only) first.
- Update the relevant openspec `tasks.md` with what was *actually* verified live, not just what was coded.
