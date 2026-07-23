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
