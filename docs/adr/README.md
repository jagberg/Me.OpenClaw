# Architecture Decision Records

## Convention: amend in place, or supersede with a new ADR?

Written down 2026-07-25. It had been practised consistently for weeks but recorded nowhere, so the next person had to infer it — and `template.md` offers only `superseded by ADR-NNNN`, which suggests supersession is the sole mechanism. It isn't.

**Amend in place** — append an `## Amendment (YYYY-MM-DD) — <what>` section — when the **decision still stands** but a premise behind it was wrong or incomplete. The original text stays exactly as written; the amendment says what was wrong, how it was found, and what changed as a result. Precedents: 0011 (the Sr format was more varied than the Context claimed), 0013 (design corrected before build), 0016 (a daily token cap it said didn't exist), 0009 (asserted "no context cap" three times).

**Supersede with a new ADR** — and set the old one's status to `superseded by NNNN` — when the **decision itself changes**. Precedent: 0001 (Gemini-only) → 0009 (provider-agnostic).

The two can combine: 0009 keeps its decision, so it is amended, and its *mitigation* for quota exhaustion is superseded by 0017 — the amendment says so and points there.

Why not always supersede: most corrections are factual, not directional. A new ADR per corrected fact would bury the handful of real reversals in noise, and the original reasoning is the part worth preserving. Never edit the original decision text to match reality — that destroys the trail, which is the whole point.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-gemini-only-llm-backend-for-v1.md) | Use Gemini 2.5 Flash (AI Studio free tier) as sole LLM backend for v1 | superseded by 0009 | 2026-07-08 |
| [0002](0002-python-fastapi-apscheduler-sqlite-stack.md) | Python/FastAPI/APScheduler/SQLite core stack, single Docker Compose service | accepted | 2026-07-08 |
| [0003](0003-dashboard-only-reminders-no-push.md) | Reminder delivery via local web dashboard only (no push notifications) for v1 | accepted | 2026-07-08 |
| [0004](0004-gmail-polling-over-push-watch.md) | Gmail ingestion via polling, not push/watch | accepted | 2026-07-08 |
| [0005](0005-windows-strict-x509-ssl-workaround.md) | Windows strict-X.509 SSL workaround for outbound HTTPS | accepted | 2026-07-08 |
| [0006](0006-claims-service-logical-boundary-single-process.md) | Claims service as a logical boundary inside the single app, not a separate deployable | accepted | 2026-07-18 |
| [0007](0007-bank-charge-ceiling-invoice-matching.md) | Bank charge as claim ceiling; claim the claimable subtotal, not the charge | accepted | 2026-07-18 |
| [0008](0008-append-only-claim-status-event-log.md) | Append-only event log for claim status, with explicit confirm-to-resolve | accepted | 2026-07-18 |
| [0009](0009-provider-agnostic-llm-backend.md) | Provider-agnostic LLM backend (Groq default), superseding 0001 | accepted | 2026-07-19 |
| [0010](0010-vision-ocr-fallback-scanned-invoices.md) | Vision-OCR fallback for scanned invoice PDFs, hard-capped attempts | accepted | 2026-07-23 |
| [0011](0011-condition-thread-correlation.md) | Petcover correlation is per Condition Thread, not per Submission | accepted (design) | 2026-07-23 |
| [0012](0012-continuation-defaults-true.md) | The claim form's continuation box defaults to ticked | accepted | 2026-07-23 |
| [0013](0013-excess-accrual-gates-submission.md) | Hold a condition's claim until its accrued claimable exceeds the annual excess | accepted (design; implementation pending) | 2026-07-24 |
| [0014](0014-durable-telegram-message-log.md) | One durable table records every Telegram message, and doubles as the replay queue | accepted | 2026-07-25 |
| [0015](0015-restart-on-dead-updater-and-alerting-levels.md) | A dead Telegram updater restarts the process; ERROR means Justin must act | accepted | 2026-07-25 |
| [0016](0016-telegram-agent-tool-surface.md) | How far the chat agent's tools reach (named mail sweeps, no repo access), and why not MCP — **the "not MCP" half is reversed by 0023/0025**; the sweeps and the no-repo-access rule stand | accepted (partly superseded) | 2026-07-25 |
| [0017](0017-per-model-daily-budget-fallback.md) | Fall through to another model's own daily budget, and disclose the downgrade — amends 0009 | accepted | 2026-07-25 |
| [0018](0018-host-side-db-access-is-read-only.md) | Host-side access to the live SQLite DB is read-only, always | accepted | 2026-07-25 |
| [0019](0019-one-invoice-several-pets.md) | One charge covering several pets → a claim per pet; the matcher apportions two invoices itself, a receipt's payment line beats the service-date window — builds on 0007 | accepted | 2026-07-27 |
| [0020](0020-re-reading-petcover-mail-may-not-write-status.md) | Re-reading already-ingested Petcover mail appends events only, never status; who owes a document comes from To:/Cc:; a vet reply is unobservable so outcome is inferred — narrow exception to 0016's no-force-reprocess, amends 0011 | accepted | 2026-07-27 |
| [0021](0021-status-is-state-label-is-who-holds-it.md) | A claim's stored status is pipeline state; its label is who holds it — one vocabulary, `matched` derived from the action determination | accepted | 2026-07-28 |
| [0022](0022-claim-status-is-a-declared-state-machine.md) | A claim's status is a declared state machine with one writer, and the event log is its source — completes 0008; three event kinds, `apply_event` the only writer, projection cached in the column | accepted (Phase 1 shipped; Phase 2 designed, not built) | 2026-07-31 |
| [0023](0023-the-agent-tool-allowlist-serves-security-and-feasibility.md) | The agent's tool allowlist serves security and feasibility at once — 32 tools to 1 cuts a turn 22,810 -> 5,355, keeping Groq viable; both properties asserted at deploy | accepted | 2026-08-01 |
| [0024](0024-openclaw-gateway-is-the-shell-python-keeps-the-domain.md) | The OpenClaw gateway is the shell, Python keeps the domain — four components, an atomic cutover, and what the fit audit kept versus replaced | accepted | 2026-08-01 |
| [0025](0025-the-proposal-gate-is-split-by-origin.md) | The proposal gate is split by origin and its text is composed by code — card taps commit behind /internal, chat proposals in the MCP surface, neither from the model's own account | accepted | 2026-08-01 |
| [0026](0026-llm-backend-routes-by-purpose.md) | Provider and model are chosen per call purpose, not per process — amends 0009's "one provider at a time"; chat starved extraction's budget, and vision must leave a quota whose terms say humans may read it | **proposed** — vision provider still open | 2026-08-02 |
