## Why

The NetBank CSV is the only way transactions enter OpenClaw — the no-bank-credentials
rule is a hard rule, so a manual export is not a stopgap, it is the design. Today that
export can only be handed over through the dashboard, which means Justin has to be at a
browser to do the one thing that starts every claim. Telegram is where he already answers
the bot, and the gateway already receives whatever he sends it.

Two smaller problems ride along. Nothing tells him **what period is already covered**, so
choosing the date range to export from NetBank is guesswork, and the safe guess (export
everything again) is the one he makes. And after an upload he has no signal that anything
happened until the next scheduled tick decides to look.

## What Changes

- A NetBank CSV sent to the Telegram bot as a **document attachment** is ingested exactly
  as a dashboard upload is: same parser, same positional 4-column format, same
  `INSERT OR IGNORE` dedupe on `(date, amount, merchant)`.
- The gateway plugin gains an **inbound-document path**. It forwards the file's bytes and
  name to the app over `/internal` and holds no parsing logic, no format knowledge and no
  claims logic — the same boundary its command handlers already keep.
- An accepted upload **immediately runs the claims scan** rather than waiting for cron,
  and does so under the same in-process lock `/internal/tick` uses, so an upload and a
  cron tick can never draft the same claims twice. *(The dashboard path calls
  `pipeline.run_once()` with no lock today; this change closes that too.)*
- The reply says what actually happened: rows read, rows new, rows already held, new
  claims found — and the **coverage watermark**, the date of the most recent transaction
  now stored.
- The coverage watermark is **derived** (`MAX(date) FROM bank_transactions`), never
  stored. It is surfaced in the upload reply and on the dashboard's upload panel.
- Failures are visible and specific: a file that is not the expected layout is refused
  with the offending row named, an unauthorized sender is refused out loud, and a document
  that is not a CSV is answered rather than silently dropped.
- **No new credential, no new stored secret, no bank connection.** This adds a second
  manual-upload channel and nothing else.

## Capabilities

### New Capabilities

None. This extends two existing capabilities.

### Modified Capabilities

- `bank-transaction-feed`: gains three requirements — a second manual upload channel
  (Telegram document), an upload triggering the claims scan immediately and reporting what
  it found, and a derived coverage watermark. The existing dashboard requirement is
  unchanged and stays true: it never claimed to be the only channel.
- `openclaw-gateway-runtime`: gains one requirement — the in-gateway plugin forwards an
  inbound document to the app without interpreting it, extending the "plugin registers and
  forwards, the app decides" boundary from commands to attachments.

## Impact

- `app/openclaw/netbank_csv.py` — unchanged parsing; likely one new derivation helper for
  the watermark and an import result that reports counts instead of a bare integer.
- `app/openclaw/internal_api.py` — one new secret-guarded route accepting the file, teeing
  an inbound row to `telegram_messages`, and running the scan under `run_exclusive`.
- `app/openclaw/main.py` — the dashboard upload moves onto the same shared entrypoint so
  the two channels cannot drift, and picks up the lock it lacks today.
- `app/openclaw/templates/index.html` — the watermark on the upload panel.
- `app/gateway-plugin/index.js` — a new inbound hook and the forward.
- `docker-compose.yml` — **possibly**, and this is the open risk: the gateway's inbound
  media directory is `<configDir>/media/inbound`, and `<configDir>/media` is mounted
  **read-only** in this deployment (verified live). See `design.md`.
- `scripts/gateway_preflight.py` — an assertion that the inbound path is live, on the same
  principle as the button-command check: a path that silently stopped working must not look
  like a quiet week.
- `app/tests/test_core.py` — parser and watermark coverage; `app/tests/test_telegram.py` —
  the forward and the refusal paths.
- No new Python dependency. No LLM call anywhere on this path (the format is positional and
  the parser is exact — this is the classification-by-regex rule, not a model's job).
