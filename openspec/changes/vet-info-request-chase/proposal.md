## Why

Petcover suspends a claim until someone sends it a missing document — usually consult notes it asks the **vet** for, with Justin only on Cc. If the vet never sends them, nothing happens: no chase, no reminder, and the claim quietly dies at the one-year submission deadline. Justin has already lost claims this way, and three of these emails arrived today.

Verified against the real mailbox and DB on 2026-07-27 (60 Petcover emails since 2024-07-01, three senders):

**The loss pathway is real and observable.**

| Evidence | What it shows |
|---|---|
| `DC1-27-5628 SR1 Request for information` → `info@kingsvet.com.au` (13 Jan 2026), then `DC1-27-5628 SR1 - Claim suspended` (29 Jan 2026), then **nothing, ever** | request → no reply → suspended → dead |
| `DC1-27-5631 SR1 Request for information` → `info@kingsvet.com.au` (13 Jan 2026) — one email, no further mention of that reference anywhere | dead on arrival |
| `ELD-25-2728 - Declined - Invoices over 12 months` (9 Aug 2024) | the loss actually realized |
| The letter's own text: *"your claim must be submitted within one year of your pet receiving treatment"* | the deadline is treatment-anchored, not request-anchored |

**A vet reply is not observable, so the outcome has to be inferred.** All ten vet-addressed request threads contain exactly one message: Justin is Cc'd, the vet replies to Petcover, and that reply never reaches his mailbox. The only available signal is whether a *later Petcover email* cites the same reference and serial.

**And today the system mishandles these emails four separate ways** — every one confirmed live, not inferred:

1. **Reference extraction breaks on non-breaking hyphens.** The letter body renders the reference as `DC1‐26‐5992` (U+2010), and `extract_reference`'s `[A-Za-z0-9-]+` stops dead at it, learning `DC1`. Consequence today: the letter about claim **#8** (Kings Vet, `DC1-26-5992 Sr 1`) failed its exact (reference, Sr) match, fell through to recency correlation, and attached to claim **#2** (The Shire Vet) — which now holds the junk reference `DC1`. The one email that flagged the right thing flagged the wrong claim.
2. **Two live Sr formats are unrecognized**: `Sr.8` / `sr.1` (dot separator — the adjacency regex expects whitespace) and `Serial Number: 2` (a third labeled form beside `Sr N` and `Treatment number: N`).
3. **The vet-addressed template classifies as `unclassified`** — its body is one sentence, its reference appears only in the subject, and the context patterns are case-sensitive (`Petcover claim for Ari DC1-27-5628 Sr.8` yields nothing). It lands in the manual-review queue, which is the one place that produces no action. Two live examples: 19 Jul, 27 Jul.
4. **The policyholder template classifies as `suspended`, never `info_requested`** — its own sentence *"Your claim will be suspended until we have the required information"* hits the `suspended` keyword, which is ordered ahead of `info_requested`. The live DB holds **zero** `info_requested` events. A genuinely distinct "Claim suspended" letter also exists (29 Jan 2026), so the escalation and the request are indistinguishable.

Nothing records **who owes the document** (`process_reply` never sees the `To:` header), and urgency is flat: `confirm_resolved` sits third in `ACTION_PRIORITY`, `pending_actions` sorts by charge date, and the daily nudge only fires past `ACTION_NUDGE_DAYS` and names the oldest action rather than the expiring one.

## What Changes

**Route these emails correctly (root causes, not the symptom)**

- Normalize Unicode hyphens/dashes (U+2010–U+2015, U+2212) to ASCII before every reference and Sr extraction. One normalization at the seam, not per-pattern.
- Extract the reference by its own shape (`DC1-26-5992`, `GABR-0305`, `ELD-25-2728`), case-insensitively, guarded against matching inside the policy number `GABR-0306-DC1-00000001R` — the reason context phrases were used in the first place. Context phrases stay as the first, highest-confidence attempt.
- Recognize `Sr.N` and `Serial Number: N` alongside the existing `Sr N` and `Treatment number: N`.
- Classify both Further-Information templates as `info_requested`, ahead of `suspended`: the request template says the word "suspended" about its own future. `requiredinfo.au@petcovergroup.com` is a dedicated channel — sender alone classifies it, no regex needed.

**Record who owes the document, and escalate on the real deadline**

- `process_reply` gains the `To:`/`Cc:` recipients. An address matching `vet_contacts` (or any non-Justin address) means the **vet** owes it; addressed to Justin means **he** owes it. Stored on the event, surfaced with the vet's clinic name and email so the card says who to chase.
- New action kind `chase_vet` (vet owes) / existing `confirm_resolved` (Justin owes), placed at the top of `ACTION_PRIORITY`, carrying **days until the treatment date's one-year deadline** rather than charge age.
- The daily nudge reports info requests regardless of `ACTION_NUDGE_DAYS`, ordered by deadline, not by charge age.
- The requested document itself (`Consultation notes dated 18/05/2026`) is captured by regex from the letter — no LLM.

**A separate register for the two-year history**

- New `info_requests` table: reference, Sr, requested date, addressee, pet, requested document, `claim_id` (nullable), outcome. Populated by the same derivation live and in backfill.
- One-off `scripts/backfill_info_requests.py` sweeps the three Petcover senders back two years. **It writes only to `info_requests`** — never to `claim_status_events` or `vet_claims`. Replaying old mail through the live pipeline would fabricate status events on current claims, and `PETCOVER_STATUS_SINCE` exists precisely to prevent that.
- Outcome is derived from later Petcover mail citing the same reference and Sr: `resolved` (acknowledged/approved/settled follows), `suspended` (a suspension letter follows and nothing after), `open` (nothing follows, treatment under a year old → actionable), `expired` (nothing follows, treatment over a year old → listed, no buttons, Justin's manual call).
- Most history cannot be linked to a claim and that is expected, not a failure: `GABR-0305`, `GABR-0306`, `DC1-26-4751`, `DC1-27-5631` and `DC1-27-5628 Sr1` predate every transaction on file (bank CSV coverage starts 2025-07-17). They exist in the register with `claim_id = NULL`.
- Dashboard section and Telegram surface split `open`/`suspended` (actionable) from `expired` (a plain list Justin handles himself).

**Non-goals, stated rather than discovered later**

- **No chase email is drafted.** Justin asked to be flagged, and the card carries the clinic's address; `invoice_matching.draft_invoice_request` is the obvious upgrade path if flagging alone still gets ignored. Sending remains forbidden either way (hard rule).
- No LLM anywhere in this change. Classification, references and requested documents are all keyword/regex, per the existing rule.
- No automatic repair of the reference already corrupted on claim #2 — a one-line `UPDATE` against the live DB, done deliberately and recorded.

## Capabilities

### New Capabilities
- `vet-info-request-register`: a standalone, claim-optional register of every Petcover information request over the last two years — who owes the document, whether the claim ever moved afterwards, the treatment-anchored one-year deadline, and the manual-only list for requests already past it.

### Modified Capabilities
- `claim-status-tracking`: both Further-Information templates classify as `info_requested` (ahead of `suspended`, and by sender for `requiredinfo.au@`); replies carry their recipients so the party who owes the document is recorded; an outstanding information request outranks every other pending action and is measured against the treatment deadline, not charge age.
- `condition-thread-tracking`: reference and Sr extraction survive Unicode hyphens, recognize the reference by its own shape case-insensitively (policy number still excluded), and accept the `Sr.N` and `Serial Number: N` formats.
- `telegram-bot`: an information-request action card names the clinic that owes the document and the days remaining, and the daily nudge surfaces these irrespective of the generic stale-action threshold.

## Impact

- **Modified code**: `claim_status.py` (hyphen normalization, `extract_reference`, `extract_sr`, `SUBJECT_KEYWORDS` order, `process_reply` recipients, `_action_kind`, `ACTION_PRIORITY`, `_ACTION_META`, register reads), `pipeline.py` (pass recipients from the polled message, `nudge_stale_actions`), `db.py` (new `info_requests` table), `main.py` + `templates/index.html` (register sections), `telegram_bot.py` + `claim_card.py` (new action kind label/colour), `config.py` (deadline + backfill knobs).
- **New code**: `scripts/backfill_info_requests.py` (one-off, read-only against claims).
- **Schema**: one new table — `CREATE TABLE IF NOT EXISTS` covers it. No `ALTER` on an existing table, so no manual live DDL beyond the deliberate claim #2 reference repair.
- **Data repair**: claim #2's `petcover_reference = 'DC1'` cleared, and event 28 re-pointed to claim #8, by hand against `app/data/openclaw.db`.
- **Third-party calls**: Gmail reads only — the same three senders already polled, with a wider `after:` window in the one-off backfill. No new API, no LLM tokens, $0.
- **Docs**: ADR for the addressee signal and the inferred-outcome rule (a vet reply is structurally unobservable), `README.md` (the lifecycle now branches on who owes the document), `app/openclaw/CLAUDE.md` (the `claim_status.py` row and the repeated-gotchas list — non-breaking hyphens belong there).
- **Tests**: `app/tests/test_core.py` — hyphen normalization, each live Sr format, the two Further-Information templates against their real text, addressee resolution, outcome derivation, and the priority/deadline ordering.
