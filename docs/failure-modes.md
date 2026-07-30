# Failure modes, and what catches them now

An index, not a diary. Every failure below is already written up somewhere — an ADR amendment, a `BACKLOG.md` post-mortem, a `CLAUDE.md` gotcha, a `tasks.md` correction, a commit message. Those stay the record; this table only points at them and answers one question the records cannot: **is there a check that would catch it again?**

Built 2026-07-29 because answering "have we seen this before?" required an agent to sweep both `CLAUDE.md` files, the whole BACKLOG, ten ADR amendments and forty commits. That sweep is the cost this file removes.

**How to use it.** When a change ships and something was wrong, add a row. Fill the third column with the *check*, not the fix: a test name, an `eval-change` preflight row, an axis clause, or the words **no check yet**. A row whose third column says "no check yet" is a standing invitation; a row that names a check is a regression that cannot silently return.

**How to keep it honest.** The third column is a claim about what exists. Verify it before writing it — this repo has a recorded instance of a task ticked for a test that covered 4 of 9 cases (`333201b`). Naming a check that does not discriminate is worse than admitting there is none.

`eval-change` = `C:\Users\jagbe\.claude\skills\eval-change\SKILL.md`. Its axes were derived from this table's contents, so the two are meant to drift apart only in one direction: new rows appear here first, and earn an axis clause later.

## Tests or verification green while the code was wrong

| What happened | Recorded in | Check that catches it now |
|---|---|---|
| Live verification passed by coincidence: the one claim checked had invoice date == charge date, hiding a charge-vs-treatment anchor bug worth 17d and 6d of false slack on two claims the feature's own filter excluded | ADR-0020 correction; `ce9150f` | `eval-change` axis 3(a) — for every verified number, list every input the code could have read and check whether they differ *in the row checked*; score capped at 3 if not. Plus `test_the_deadline_is_anchored_on_treatment_not_on_the_bank_charge` |
| Unit tests green while a dry run against real mail produced `information in order for us to review the` as the document to chase | ADR-0021 amendment; `719009f` | `test_the_ask_own_filler_is_not_mistaken_for_the_document`; `eval-change` axis 2 (would it fail if the behaviour broke) |
| A task ticked claiming a per-kind assertion; 4 of 9 kinds asserted, and the two the refactor moved had none | BACKLOG; archived `clarify-claim-status-vocabulary` tasks.md; `333201b` | `eval-change` axis 2(d) — count the domain's members, count the assertions, name the unasserted ones. **The underlying gap is still open** (no per-kind test exists) |
| Two fixtures could never reach their assertion and had been failing for four commits, unrun because root `CLAUDE.md` names only `test_core.py` | `d3bab87` | `eval-change` preflight rows 3-4 run **both** suites; axis 2(c) and 2(e) |
| Adding a fourth model to the fallback chain made a negative test silently pass, because the test hardcoded the three-model set | `3cb9b15` | Test now derives the set from `_FALLBACK_MODELS`; `eval-change` axis 2(b) |
| "Tested" conflated with "exercised against reality" — `split_between_pets` unit-tested, never run on a real bill | BACKLOG | `eval-change` axis 2(f) and 3(c) |
| A test that asserts its own input: `for target in targets: assert target in TRANSITIONS[from_state]` where `targets` **is** that set. True of any table including an empty one; 0 of 42 pairs actually checked, and a task ticked on it | `1f49871`; eval 2026-07-30 | Rewritten to drive every pair through `apply_event`; **mutation-checked** — refusing a legal pair now fails the suite. `eval-change` axis 2(a) found it |
| A test named for a case it never constructs — "survives an illegal one" injected only legal events, so the fold's skip-and-continue was asserted by nothing. Mutating it to `break` left the whole suite green | `1f49871`; eval 2026-07-30 | Now asserts the pair is illegal before relying on it, and the mutation fails. `eval-change` axis 2(c) |
| A measurement whose two sides come from the same source: the 19→0 shadow count. The backfill copies `status` into `detail`, the fold reads it back exempt from the table, and stripping backfill events projects `pending_match` for all 22 — so a projection ignoring every real event scores identically | BACKLOG; eval 2026-07-30 | `eval-change` axis 3(a). **No general check** — the axis brief catches it by asking what each side derives from |

## Real data versus assumptions

| What happened | Recorded in | Check that catches it now |
|---|---|---|
| A host-side `db.get_connection()` silently opens a phantom `C:\data\openclaw.db` holding 2 stale claims, returning plausible wrong rows with no error | root `CLAUDE.md`; BACKLOG | `eval-change` preflight row 7 and axis 3(h). **No mechanical guard** — BACKLOG asks for a hook |
| Measuring the obvious surface gave a confident wrong answer: rate-limit headers report TPM/RPD only; the 100k daily cap appears solely in a 429 body. ADR-0009 asserted "no context cap" three times | ADR-0016 and ADR-0009 amendments; `0e5f7b1` | `eval-change` axis 3(e). No test — this is a research-method failure |
| One sample generalised into a format rule, so an event routed to all 3 claims of a thread instead of one (`Treatment number: 2` carries no `SR`) | ADR-0011 amendment | `test_*` per live serial format; `eval-change` axis 3(f) |
| The prompting case was diagnosed wrong and tasks written on a false premise (one shared invoice, actually two separate ones) | ADR-0019 amendment; `b0d7923` | `eval-change` axis 3 buckets. **No check** for "was the premise itself confirmed against primary documents" |
| Partial inspection reported as fact — "neither receipt has a PDF", from listing top-level MIME parts only | `54dfa84` | `eval-change` axis 3(g) |
| Stored data drifted from the fixed code: re-running the corrected classifier over 20 stored events found 6 disagreements, two info requests producing no action for over a week | `3cec228` | `eval-change` axis 3(d) |
| A capability shipped whose premise has no instance in the real data (3 line-item dates found, all equal to their invoice header date) | `9096b5e`; tasks.md | `eval-change` axis 3(c) |
| Bucketing by "now" instead of the record's own date; two transactions straddling a policy anniversary were processed the same week | ADR-0013 amendment | `test_visit_ledger_uses_anniversary_year_not_calendar_year`. **No general check** |
| LLM non-determinism: re-extraction returned 9, then 10, then 8 invoices for the same emails | `9096b5e` | `eval-change` axis 5(f). Guard is the keep-if-empty rule in the script, not in the app |
| The agent fabricated arguments from a tool schema's own description strings, and a non-action ("redo claim #7" -> created two tasks) read like an action | `b809da6`; BACKLOG | `test_replied_to_claim_id_refuses_to_guess`; every `propose_*` takes a `claim_id` |

## Process and decision trail

| What happened | Recorded in | Check that catches it now |
|---|---|---|
| Five documents asserted a "treatment-anchored" rule the code did not implement, for about a day | ADR-0020 correction; `294d319`, `ce9150f` | `eval-change` axis 4(a) — count and name them — and axis 1(c), which rejects a requirement naming a rule by label without stating which value it means |
| A real architectural decision lived only in a code comment, and the comment cited the wrong ADR | `8b70915` | `eval-change` axis 4(b) |
| `ADDED` used where `MODIFIED` was required — archiving would have put two contradictory requirements in the baseline | `333201b` | `eval-change` axis 1(b) |
| Baseline rot: five shipped-but-unarchived changes meant their deltas never reached `openspec/specs/` | `1c6c270` | `eval-change` axis 4(e); `openspec/BACKLOG.md` exists for exactly this |
| An accepted ADR edited in place, against `docs/adr/README.md`'s own convention | ADR-0019 process note; `df64156` | `eval-change` preflight row 8 — `git diff` on any `accepted` ADR touching lines above its first `## Amendment` |
| A safety property silently downgraded from scope-enforced to convention-enforced: Gmail scope became `compose`, which grants send | `1c6c270` | `eval-change` axis 5(d) |
| Verification recorded as complete when partial — including a correction that itself needed a correction | BACKLOG (ADR-0018 entries) | `eval-change` axis 4(d) |
| Scope grew inside `tasks.md` without being declared in `proposal.md` | `8b70915` | `eval-change` axis 4(f) |
| A design doc contradicting **itself**, with the dangerous half winning: Decision 1 listed `state_backfilled` as stateless, Decision 8 had it carry the claim's status. Stateless wins in a fold, so the backfill would have moved nothing and Phase 2 would then have reset 19 claims | `8e1f852`; `design.md` Decision 1 correction | **No check yet.** Found by implementing it. `eval-change` axes 1 and 4 compare docs against *code*, not a doc against itself |
| A correction applied to the verification record but not to the artifacts that outlive it — `tasks.md` 1.3 was right on 2026-07-29 while the delta spec, `design.md` Decision 3 and `README` still asserted the withdrawn claim. The delta is what `sync` copies into the baseline | `1f49871`; eval 2026-07-30 | `eval-change` axis 1(A) and 4(a). Rule: correct the delta spec first, since it is the only one that becomes permanent |
| A destructive step left unconditional when the write after it became refusable: `unmatch` wiped the invoice, then had its `pending_match` write refused for a submitted claim, stranding it with no invoice in `sent`. Shipped, deployed and reachable from a Telegram button; no test called `unmatch` at all | eval 2026-07-31 | `claim_status.transition_allowed` asks the table *before* destroying; `test_unmatching_a_submitted_claim_destroys_nothing`, **mutation-checked**. General rule: when routing a write through a refusable path, move the destroy behind the same question |
| Coverage asserted from memory: an accepted ADR's amendment said a retraction was "corrected in each" of four documents when two still carried it — including the delta spec, the only artifact that becomes permanent | ADR-0020 amendment 2026-07-31 | **No check yet.** Enumerate the sites and grep each; `eval-change` axis 4(a) catches it one round later, which is how this was found |
| A guard reporting a different thing than it acts on — the phantom-DB check printed `os.environ["DATABASE_PATH"]` after the dotenv load, displaying the container path while opening `C:\data\openclaw.db` | `1f49871`; eval 2026-07-30 | Guard now resolves the path the way the connection does. **Its own weakness is stated in the docstring**: only the row-count condition actually fires |

## Operational

| What happened | Recorded in | Check that catches it now |
|---|---|---|
| A host-side read-write open of the live DB deleted the WAL sidecars and took the container down for 51 minutes. Rule broken four times in one later session | ADR-0018; BACKLOG | `eval-change` preflight row 7. **No mechanical guard yet** — the highest-value one outstanding |
| The alert path reads the DB before sending, so a DB outage silences its own alarm; ERROR fired every tick and reached nobody | ADR-0015 amendment; BACKLOG | `eval-change` axis 5(b). **Still unfixed in code** |
| All four fallback models are one provider, so a 403 took invoice extraction and the chat agent down together | BACKLOG; `9096b5e` | `eval-change` axis 5(c). **Still unfixed** — Gemini exists as a cross-provider fallback |
| Deploy worktree tracked a feature branch and shipped code four commits behind master; a bare `docker compose up` leaves `APP_VERSION=unknown`, mistagging every `telegram_messages` row | root `CLAUDE.md`; `333201b` | `eval-change` preflight row 9 |
| A fire-and-forget task's death is silent; conversely an awaited replay blocked the port from binding and faked a failed deploy | ADR-0014 amendment | `eval-change` axis 5(e); `polling_alive()` + the watchdog |
| A local resource bug presented as a provider outage — a write transaction held across LLM calls surfaced as `database is locked` | `9096b5e` | `eval-change` axis 5(f). Script restructured read -> call -> write |
| Retry classification too coarse in both directions: one 403 ended a chat turn; per-minute and per-day limits need opposite responses and only the 429 body says which | `b809da6`; `app/openclaw/CLAUDE.md` | `eval-change` axis 5(i); `llm.py` classification tests |
| A latent bug exposed only when a new path reached it — `reasoning` echoed back into the tool loop, 400ing every later request. Fixed with a whitelist, not a blacklist | `199cc20` | `test_*` on the whitelist. **No general check** for "a new path reaching old code" |
| `CREATE TABLE IF NOT EXISTS` never alters an existing table, so live schema changes need manual DDL | root `CLAUDE.md` | `eval-change` axis 5(g) |
| Anything constructing a plain `telegram.Bot` bypasses the message log entirely | `app/openclaw/CLAUDE.md` | `eval-change` axis 5(h) |

## Standing gaps

Rows above whose third column admits there is no check, ranked by damage already done:

1. **No mechanical guard on host-side live-DB access.** Caused the only total outage; the rule has been broken repeatedly by people who had just read it. ADR-0018 says plainly: *"Nothing prevents the next plain `connect()`."*
2. **The alert path can still be silenced by the outage it should report.** Known, recorded, unfixed.
3. **The fallback chain is still single-provider.**
4. **No per-kind test for `_action_kind`** — the gap `333201b` measured at 4 of 9.
5. **No check that a change's premise was confirmed against primary documents** before tasks were written on it. This is how ADR-0019 came to describe the wrong shape, and it is the hardest of these to mechanise.
