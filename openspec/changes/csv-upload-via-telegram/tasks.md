## 1. Spikes — nothing else starts until these return

- [ ] 1.1 Send one real NetBank CSV to the bot as a document, with a temporary plugin
      handler on **`message_received`** that logs the whole event and its `metadata`. Record
      whether `metadata.mediaPath` is present, whether the file exists at that path, and
      whether the agent also took a turn on the message.
- [ ] 1.2 Repeat 1.1 with a handler registered on **`inbound_claim`**. Record whether the
      handler is invoked at all — the only call site found in the shipped bundle is the
      plugin-conversation-binding variant, so "never fired" is a real possible outcome and
      must be distinguished from "fired with no media".
- [ ] 1.3 Repeat 1.1 with a handler on **`before_dispatch`**, the hook this repo already
      uses live. Record whether its event carries media metadata, and whether it is reached
      at all for a document with no caption (the existing handler returns early on empty
      text).
- [ ] 1.4 Write up 1.1–1.3 in `design.md` Decision 6 as measured behaviour, and pick the
      hook. If none delivers a document, stop and report — do not build a path that works
      only for a captioned file.
- [ ] 1.5 Confirm the read-only failure: check the gateway log while 1.1 runs for an inbound
      staging error (`EROFS` / `mkdir … media/inbound`). This is predicted, not observed —
      no document has ever been sent to this bot.
- [ ] 1.6 Relocate the outbox mount to `<configDir>/media/outbox` in a scratch compose,
      bring the gateway up, and **send one real claim card from the new location**. This is
      the only thing that settles whether the media root allowlist accepts a subdirectory.
      Note that `media_outbox.publish()` returns the gateway-side path and changes with it.
- [ ] 1.7 If 1.6 fails: check whether the plugin hook exposes anything that yields a
      Telegram `file_id`, which would let the plugin download from the Bot API directly and
      remove the media store from the design. Record the answer either way.

## 2. Make the media directory writable

- [ ] 2.1 Update `docker-compose.yml` per the outcome of 1.6: gateway state writable at
      `<configDir>/media`, shared `media_outbox` bound read-only one level down.
- [ ] 2.2 Update `media_outbox.publish()`'s returned gateway path and its module comment —
      the comment states the mount point as a fact and must not become a lie.
- [ ] 2.3 Verify outbound is unbroken end to end: an `/actions` run delivers its rendered
      summary card, from the running pair, not from a unit test.

## 3. The app's ingest entrypoint

- [ ] 3.1 Add `netbank_csv.latest_transaction_date()` — `SELECT MAX(date) FROM
      bank_transactions`, returning `None` for an empty table. No column, no table, no
      stored value.
- [ ] 3.2 Change `import_rows` (or add a thin wrapper) to report `(read, inserted,
      skipped)` rather than a bare inserted count — the reply has to state all three.
- [ ] 3.3 Add the one shared entrypoint both channels call: parse → import → scan under
      `internal_api.run_exclusive("tick", pipeline.run_once)` → build the reply text
      including the watermark. Lock name must be exactly `"tick"`.
- [ ] 3.4 Handle the three outcomes distinctly and visibly: imported+scanned;
      imported+scan-already-running (`ran=False`, never reported as completed);
      imported+scan-raised (partial success, never reported as plain success).
- [ ] 3.5 Route `CsvParseError` into a reply naming the offending row. Nothing partial is
      ever inserted — assert this against a file whose fifth row is malformed.

## 4. The internal route

- [ ] 4.1 Add `POST /internal/transactions/csv` to `internal_api.py`, secret-guarded and
      host-allowlisted like every other route on that surface.
- [ ] 4.2 Authorize the sender with `commands.is_authorized`. An unauthorized document is
      refused, logged and answered — never silently dropped.
- [ ] 4.3 Tee an inbound row with `tee_inbound` **before** the work, settle it after with
      `settle_inbound`, annotating any failure — same shape as `/internal/command/{name}`.
- [ ] 4.4 Keep the route a thin wrapper: it decodes, authorizes, calls 3.3, returns text.
      No parsing and no claims logic in this module, per its own docstring.

## 5. The dashboard joins the same entrypoint

- [ ] 5.1 Rewrite `main.upload_transactions` to call 3.3 rather than
      `netbank_csv.parse` + `import_rows` + a bare `pipeline.run_once()`. This is what
      closes the existing unlocked-concurrent-tick defect on that path.
- [ ] 5.2 Surface the row counts and the scan outcome on the dashboard, not just the
      existing `upload_error` redirect — a skipped or failed scan must be visible there too.
- [ ] 5.3 Show the coverage watermark on the upload panel in `templates/index.html`; say
      "no transactions held" when the table is empty rather than rendering a blank.

## 6. The plugin

- [ ] 6.1 Register the hook chosen in 1.4. Detect a document, read the staged file with
      `node:fs`, and POST `{filename, content_b64, username, chat_id}` through the existing
      `callApp` with a correlation id.
- [ ] 6.2 Return the app's reply text. On a forward failure, say the file did not reach the
      app and why — never a bare success and never silence.
- [ ] 6.3 Ensure a handled document does not fall through to the agent. Verify by checking
      no model turn was recorded for that message, not by reading the handler.
- [ ] 6.4 Keep the plugin free of format knowledge: no CSV parsing, no column layout, no
      authorization decision. The existing "plugin carries no domain logic" scenario covers
      this and the source is the evidence.

## 7. Preflight and tests

- [ ] 7.1 Add a preflight assertion that the inbound-document path is live, following
      `check_button_commands`' principle — a silently broken path must not look like a week
      with no uploads.
- [ ] 7.2 `test_core.py`: watermark derivation (empty table, single row, overlap that does
      not advance it); the three scan outcomes of 3.4; parse failure inserting nothing.
- [ ] 7.3 `test_core.py`: assert no stored watermark exists — nothing outside
      `bank_transactions` answers "latest transaction date".
- [ ] 7.4 `test_telegram.py`: the internal route's authorized, unauthorized and
      malformed-body paths, and that an inbound row is teed and settled for each.
- [ ] 7.5 Run **both** suites (`test_core.py` and `test_telegram.py`) and `ruff format` +
      `ruff check` over `app` and `scripts`; `check` is clean today and stays clean.

## 8. Live verification and documentation

- [ ] 8.1 Send a real NetBank export to the bot. Confirm the rows land, the scan runs, and
      the reply states counts, claims found and the watermark.
- [ ] 8.2 Send the **same file again**. Confirm nothing is inserted, no claim is duplicated,
      and the watermark is unchanged and said to be unchanged.
- [ ] 8.3 Send a non-CSV document and a malformed CSV. Confirm both are refused with a
      reason Justin can act on.
- [ ] 8.4 Upload from the dashboard and confirm identical behaviour — the two channels are
      one entrypoint or they are not.
- [ ] 8.5 Record in this file what was verified **live** versus what is only covered by
      tests, per the repo's working-style rule.
- [ ] 8.6 Update `README.md` (the CSV upload is no longer dashboard-only),
      `app/openclaw/CLAUDE.md` (`netbank_csv` and `internal_api` rows), and the root
      `CLAUDE.md` if the compose mount changed. Add an ADR only if 1.6 forces a real
      decision about the media boundary — the mount is the kind of thing the next reader
      will otherwise assume is arbitrary.
- [ ] 8.7 Sync both capability deltas into `openspec/specs/` before archiving, or the
      baseline rots.
