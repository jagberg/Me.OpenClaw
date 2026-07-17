## 1. Prerequisites (Justin, manual)

- [ ] 1.1 Confirm manual-CSV workflow is acceptable, or pick an alternative (SMS-forwarding automation / paid aggregator)
- [x] 1.2 Supply the real pet-insurance claim template file and insurer claim-submission address — done: Petcover, real fillable AcroForm PDF (Petcover-AU-Claim-Vet-EN-V20211201), claims.au@petcovergroup.com; file placed at `app/data/petcover-claim-template.pdf` (gitignored, same as other `data/`)
- [x] 1.3 Re-run Gmail OAuth consent with the widened scope (`gmail.readonly` + `gmail.send`), producing a new `token.json` — done; hit a second, distinct SSL issue along the way (see `ssl_compat.py`/learned skill update: certifi's CA bundle was missing the corporate root entirely, not just failing the strict-X509 check)

## 2. Data layer

- [x] 2.1 Add `bank_transactions` table (date, amount, merchant, category, vet_flag, created_at, unique on date+amount+merchant for dedup)
- [x] 2.1a Add a `pets` lookup (name, insurer, claim email/process — seed with Aari/Petcover known; Echo/Bow Wow Insurance left with process fields null until Justin supplies them)
- [x] 2.2 Add `vet_claims` table (transaction_id FK, pet_id FK, matched_email_id, status: pending_match/matched/drafted/submitted_by_user, created_at, updated_at)
- [x] 2.3 Extend `db.init_db()` to create the two new tables

## 3. Bank transaction feed

- [x] 3.1 Add a dashboard CSV upload endpoint for NetBank exports
- [x] 3.2 Add `openclaw/netbank_csv.py`: positional (no header) parser for the confirmed 4-column NetBank CSV format, trims fixed-width-padded merchant/location text, inserts new transactions deduped by date+amount+merchant — overlapping re-uploads are the normal case, not an edge case
- [x] 3.3 Surface parse failures (unrecognized CSV layout) visibly (log + dashboard), matching the existing Gemini-failure-visibility pattern

## 4. Vet payment detection

- [x] 4.1 Add keyword/allowlist heuristic classifier for merchant name/category
- [x] 4.2 Add Gemini fallback call for ambiguous merchants, logged in `llm_calls` like existing extraction calls
- [x] 4.3 Write unit tests: obvious-vet, obvious-non-vet, ambiguous-triggers-Gemini
- [x] 4.4 Add a dashboard pet picker (Aari/Echo) for vet-flagged transactions with no pet assigned yet — blocks claim-form automation (not invoice matching) until answered

## 5. Invoice matching

- [x] 5.1 Add Gmail search for vet-flagged transactions (merchant + ±3 day window) — also searches a configurable `SPOUSE_EMAIL` as a fallback (invoices are sometimes forwarded from a spouse's address rather than sent by the vet directly), confirmed live: Justin's wife forwards vet receipts from her own address
- [x] 5.2 Add Gemini extraction of structured invoice fields from the matched email/attachment text — attachment text extraction was genuinely missing until tonight: a real forwarded receipt had the invoice amount only in a PDF attachment (none in the body), so `invoice_matching._full_message_text()` now pulls text out of `application/pdf` attachments via `pypdf` before extraction; image attachments (PNG/JPG) are skipped, no OCR
- [x] 5.3 Add amount-tolerance check; leave `pending_match` on mismatch or no candidate found — tolerance is now percentage-based (3%, min 1c), not a flat cent: live testing found a real invoice ($580.74) legitimately differs from the bank-charged amount ($585.39) by a ~0.8% card surcharge the vet's payment processor adds, which a flat-cent tolerance couldn't absorb
- [x] 5.4 Surface `pending_match` claims on the dashboard as a manual-follow-up list
- [x] 5.5 When still unmatched past the normal window, draft (never auto-send) an invoice-request email to the vet; on send, switch subsequent matching passes to an open-ended "transaction date → now" search instead of the fixed ±3 day window — note: vet email address is sourced from a prior matched invoice's From header (bank CSV carries no contact info); flags "no vet email on file" if the vet has never matched before, rather than guessing an address

## 6. Claim form automation

- [x] 6.1 Pick fill library for Aari/Petcover — done: `pypdf`, real Petcover form is a fillable AcroForm (39 total fields; 13 mapped and filled — see 6.2)
- [ ] 6.0 [Blocked on Justin] Echo/Bow Wow Insurance claim process — template format, submission method (email vs portal), required fields all unknown until Justin clarifies with them; claim-form automation only branches to Petcover's path for now, Echo claims stay at `matched` with a "process not yet defined" flag
- [x] 6.2 Implement template fill from transaction + extracted invoice data using the confirmed field map (Aari/Petcover only for now), save generated file; leave the condition field unset (see 6.2a) rather than guess it — `claim_forms.FIELD_MAP` verified against the real PDF (field names + on-page position cross-checked against printed labels), fill tested against the actual file; a missing-field-name mismatch raises `ClaimFillError` rather than filling blind. Update 2026-07: Justin explicitly supplied and asked to auto-fill the previously-blank fields — pet DOB (`pets.dob`), other-insurer answer (`pets.insured_elsewhere`, defaults No), bank payment details (`OWNER_BANK_*` config), and the declaration tick+date; these are now filled automatically. "Continuation of a previous claim" is a per-claim judgment call, not a stored fact — passed as an explicit `continuation` argument to `process_claim`/`process_claim_batch` each time, left blank if omitted
- [x] 6.3 Implement Gmail draft creation (attach filled form, address to insurer) — draft only, never send
- [x] 6.2a Add a dashboard field for Justin to manually enter the claim's "condition" text (interim path — chat-based entry + per-pet condition history is deferred, see `claim-form-automation` spec)
- [x] 6.4 Advance `vet_claims.status` through matched → drafted, staying at `matched` if a required field (including condition) is missing

## 7. Dashboard

- [x] 7.1 Add a claims section: pending_match / matched / drafted lists with transaction+invoice details side-by-side
- [x] 7.2 Link drafted claims to the actual Gmail draft for Justin to review and send

## 8. Verification

- [x] 8.1 Smoke test: upload a fake NetBank CSV, confirm rows parse and flag correctly — `tests/test_core.py::test_netbank_csv_parses_and_dedups_on_reupload`
- [x] 8.2 Live test: a real NetBank CSV export produces real vet-flagged rows, re-upload doesn't duplicate — main export (620 rows) plus 3 chained older exports covering back to 17 Jul 2025 (616+624+133 rows) all parsed and deduped cleanly, 1979 total transactions now on file; 22 real vet transactions correctly flagged across 6 vets (Shire Veterinary, Bankstown Vet, Kings Vet, MediPaws, SAH Stanmore, Vets Love Pets)
- [x] 8.3 Live test: a real invoice email gets matched and extracted correctly — true positive confirmed: claim #2 (Shire Vet, Aari, 19 Jun 2026) matched against a real receipt forwarded from Justin's wife, patient name/date/line items all correctly read out of the PDF attachment, amount matched through the new surcharge tolerance ($580.74 invoice vs $585.39 charged); found and fixed 3 real bugs along the way — merchant query included bank-descriptor noise (city/state) as an exact phrase, invoice extraction never read PDF attachments (only body text), amount tolerance was a flat cent instead of percentage-based
- [x] 8.4 Live test: claim template fills correctly and a real Gmail draft is created (not sent) — done for real: 3 real Aari invoices (Jul-Sep 2025, sourced from locally-saved invoice PDFs, one split out of a combined 2-pet bank charge) bundled into one claim document via the new `claim_forms.process_claim_batch()` (up to 4 invoice rows per document, confirmed against a real past submission) and one real Gmail draft to claims.au@petcovergroup.com — all field values verified correct after fill (dates, split amount, condition, owner/policy/pet info)
