# Architecture Decision Records

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
| [0016](0016-telegram-agent-tool-surface.md) | How far the chat agent's tools reach (named mail sweeps, no repo access), and why not MCP | accepted | 2026-07-25 |
| [0017](0017-per-model-daily-budget-fallback.md) | Fall through to another model's own daily budget, and disclose the downgrade — amends 0009 | accepted | 2026-07-25 |
