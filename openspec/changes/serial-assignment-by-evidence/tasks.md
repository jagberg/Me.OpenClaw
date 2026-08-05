## 1. Route by the amount the letter states

- [x] 1.1 `stated_claim_amount(text)` — one reader for the three templates that state an amount (approval `Total amount claimed:`, older settled `Amount Claimed`, under-excess `Amount claimed:`).
- [x] 1.2 `_APPROVAL_PATTERNS["claimed_amount"]` accepts the under-excess spelling. That template previously yielded no figures at all.
- [x] 1.3 `_claim_for_sr(pool, stated_amount)` returns the single claim worth the stated amount, or **None** when nothing matches or two things do.
- [x] 1.4 `process_reply` records an unassignable letter as an unlinked event flagged `needs manual link`, naming the stated figure and the candidate claim ids.
- [x] 1.5 The ordering heuristic survives only when the letter states no amount, and still records `sr_assigned_by`.

## 2. Tests

- [x] 2.1 `test_a_serial_letter_attaches_by_the_amount_it_states` — the letter's amount beats transaction order, with the older claim left unserialized.
- [x] 2.2 `test_a_letter_whose_amount_matches_no_claim_is_left_for_manual_link` — the live 2026-08-05 failure: $55.74 letter, $2,521.46 claim, no serial taken, no status moved, flag names both.
- [x] 2.3 `test_the_under_excess_letter_gives_up_its_amount` — extraction of the template that yielded nothing.
- [x] 2.4 `test_an_acknowledgement_still_routes_and_still_says_it_guessed` — the no-amount path is unchanged and still marked as inferred.
- [x] 2.5 Suite green (`ALL TESTS PASSED`), run with the main checkout's interpreter — this worktree has no `.venv`.

## 3. Verify against the live corpus

- [x] 3.1 Ran all eleven live letters through the new `_claim_for_sr` against a `src.backup()` copy, with every claim's serial stripped — the state as it was before any serial was learned. **Six routed, all six correct** against the map Petcover's own treatment dates give; **five refused**; **none wrong**. The heuristic this replaces was 0 for 10.

| letter | states | routed to | correct? |
|---|---|---|---|
| `5628` Tr 5 | $446.50 | claim #8 | ✓ |
| `5628` Tr 7 | $132.50 | claim #7 | ✓ |
| `5628` Tr 8 | $580.74 | claim #2 | ✓ |
| `5992` Tr 1 | $351.50 | claim #6 | ✓ |
| `5992` Tr 3 | $2,521.46 | claim #12 | ✓ |
| `5993` Tr 1 | $944.50 | claim #13 | ✓ |
| `5628` Tr 2 | $35.00 | refused | ambiguous — #1 and #19 are both $35.00 |
| `5992` Tr 2 | $35.00 | refused | ambiguous — same pair |
| `5628` Tr 6 | $45.00 | refused | ambiguous — #18 and #22 are both $45.00 |
| `5992` Tr 4 | $135.00 | refused | not a claim of ours (it is a line item on #2's invoice) |
| `5628` Sr 4 | $55.74 | refused | not a claim of ours — **the letter that broke #12** |

  The three ambiguous pairs are resolvable only by treatment date, which the letters do not carry and Petcover's status table does. Refusing is the correct answer there, not a shortfall.
- [x] 3.2 `DC1-27-5628` Sr 4 ($55.74) routes to nothing, so it can no longer take a $2,521.46 claim's serial.
- [ ] 3.3 Deploy, then re-check that no claim's serial changed as a side effect — this change routes new letters and rewrites nothing.

## 4. Not done, deliberately

- [ ] 4.1 **Condition-thread gate.** Petcover states `Condition:` on every letter, and `DC1-27-5628` is Arthritis while claim #12 is an ALT workup — that alone would have caught the failure. Not built: the amount rule covers every failure in the live corpus and needs no new extraction. Worth doing if a letter ever states an amount that two claims share.
- [ ] 4.2 **Capture what the one-off table told us, and stop planning around a feed.** Corrected 2026-08-06 (Justin): the 2026-07-29 table was a **one-off he asked Petcover for**, not something that arrives on any schedule. The earlier version of this item proposed a `petcover_serials` table kept fresh from their mail, which has no source to be fresh from — the premise was wrong. What is worth doing is the opposite shape: transcribe the treatment dates that reply already gave us onto the claims they identify, because that knowledge currently exists only as one email and decays with the mailbox. Refreshing it is a human asking Petcover again, which is a workflow step, not a feature.
- [ ] 4.3 **Re-read noise.** Re-reading old acknowledgements against settled claims produced transition-refusal flags on six claims, masking the real findings underneath. Expected behaviour, but a deliberate `reread=True` could suppress refusal flags it knows are historical.
