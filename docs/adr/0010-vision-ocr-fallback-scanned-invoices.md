# ADR-0010: Vision-OCR fallback for scanned invoice PDFs, hard-capped attempts

**Date**: 2026-07-23
**Status**: accepted
**Deciders**: Justin

## Context

Kings Vet (and potentially other clinics) email invoice bundles as **photo scans**: each PDF page is one embedded JPEG, no text layer at all. The text pipeline (`pypdf` extract → `llm.extract`) gets zero characters, so six real claims sat stuck on `invoice attachment unreadable` flags with the only path forward being "ask the vet for text copies". microsoft/markitdown was evaluated earlier and rejected with evidence: it also extracts 0 characters from these files — they contain no fonts, only images; nothing short of OCR reads them.

Two constraints shaped the solution:

1. **Token budget is real.** Justin: "don't burn through tokens if it can't get it right through 2-3 turns once it's productionised." The vision provider (Gemini free tier) is the scarcest resource in the system.
2. **Provider reality.** This Groq account (the default text LLM per ADR-0009) exposes **zero** vision models — verified via `models.list` (no llama-4, no vision variants). Gemini 2.5 Flash is the only configured backend that accepts images, and it was verified live before any code was written: page 1 of the real Aari bundle came back as invoice 184556 / 2025-07-28 / patient Aari / $45.00 — an exact match for claim #22's bank charge.

## Decision

**Seam** (ADR-0009 pattern): `llm.extract_vision(prompt, image_jpeg)` is the only entry point; it routes to `gemini.extract_image` regardless of `LLM_PROVIDER`, because Gemini is the sole vision-capable backend. If Groq/OpenAI gain usable vision later, only `llm.py` changes. Gemini's existing rate limiter and `llm_calls` logging apply unchanged.

**Trigger — narrowest possible.** Vision runs only where the `invoice attachment unreadable` flag used to be set: a vet-addressed candidate email with a PDF attachment whose full text contains no dollar amounts. Normal text PDFs never reach vision.

**Extraction shape.** Scans are read page-by-page (`pypdf` pulls the embedded JPEG straight out — no rasterizer dependency; pillow downscales to ≤1600px). Each page gets a single-invoice JSON prompt with a `{"not_invoice": true}` escape hatch for cover letters/statements. Every extracted invoice records `source_pdf` + `page`, because a scan has no text layer for the downstream segmentation step — `ensure_invoice_file` slices the claim's exact page by index instead of by text search, and the extracted `patient` field assigns the pet (a printed fact, not a guess; a blank patient field still asks Justin — hard rule preserved).

**Token budget enforcement:**

- `vision_ocr_attempts` table: **max 3 attempts per email**, one consumed per try regardless of outcome. After 3 failures the email goes permanently quiet with the unreadable flag standing (the ask-the-vet path remains the terminal fallback).
- **Outage refund**: an `LLMUnavailableError` mid-extraction (hit live — Gemini 503 on the first production tick) refunds the attempt. Provider downtime is not evidence the scan is unreadable, and without the refund two 503 spikes would permanently exhaust an email's budget.
- **Success caches forever** in `email_extractions` (same cache as text extraction), so vision never re-reads an email. A failed round is deliberately *not* cached, letting the remaining attempts retry next tick.

**Matching integrity (found by live verification, not review).** The first production run false-matched claim #20 ($152.50 charge) to claim #21's already-acknowledged $44.75 invoice — under the ceiling (ADR-0007 allows invoice ≤ charge) and 3 days off, while the exact $152.50 same-day invoice sat unpicked in the other pet's bundle. Bulk scans surface many small invoices at once, which makes first-fit picking dangerous. Two rules added to `_pick_invoice` (they protect text matching equally):

- **Never match an invoice another claim already carries.** Identity is `invoice_number` when both sides have one (two distinct $45 Pentosan visits share amount, never number); amount+date otherwise. A claim never blocks itself, so unmatch→rematch works.
- **Rank candidates by closest amount, then closest date** — an exact match beats another visit's smaller invoice that also clears the ceiling.

## Alternatives considered

- **markitdown** — rejected with evidence: 0 chars on these scans (no text layer exists to convert).
- **Local OCR (tesseract/PaddleOCR)** — no per-call token cost, but a heavy native dependency in the container, materially worse accuracy on phone-photo scans, and another component to operate. The vision model read every test page correctly on the first try.
- **Always ask the vet for text copies** — still the terminal fallback (and the only option for the one invoice Kings Vet omitted from the bundles entirely), but it made Justin the OCR layer for six claims that software can read.

## Consequences

### Positive
- 5 of the 6 stuck Kings Vet claims matched on the first production run, each with its own one-page invoice PDF sliced from the scan and (where the scan printed it) the pet auto-assigned.
- Worst-case spend is bounded and small: ≤3 attempts × pages-per-email, once per email ever, only for image-only PDFs from vets.
- The already-claimed guard + closest-amount ranking fix a latent false-match class that predates vision (any bulk reply with several small invoices).

### Negative / Risks
- Gemini free-tier quota (~20 req/day observed) is the bottleneck: one 11-page bundle is 11 calls. Acceptable at single-user volume; the cap and cache keep it from compounding.
- Vision output is trusted for amount/date/patient. Mitigations: the ceiling + date-plausibility gates (ADR-0007) still apply, `_already_claimed` blocks double-use, and required fields Justin must confirm (condition) are still never inferred.
- A scan the model misreads consistently burns its 3 attempts and falls back to the flag — by design, per the token constraint.

### Testing
The suite stays hermetic: all LLM env keys are force-blanked (including `GEMINI_API_KEY` — previously `setdefault`, now hard-assigned so a container env can't leak a real key into tests) and every vision test stubs `llm.extract_vision`. Scan fixtures are pillow-generated image-only PDFs — structurally identical to real photo scans, so the pypdf image-extraction path is genuinely exercised without any network call.
