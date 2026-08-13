## MODIFIED Requirements

### Requirement: An info-requested or suspended claim stays flagged until Justin explicitly confirms it resolved, or the claim settles clean
A new event arriving on a claim (even settled or declined) SHALL NOT automatically clear its "needs your action" status, so a claim isn't silently dropped when Petcover's own follow-through is inconsistent (real pattern observed: repeated "request for X" emails on the same claim before resolution). The claim SHALL leave the action list either when Justin explicitly confirms it resolved via the dashboard, or automatically, when both of the following hold: the claim reaches `settled`, and its settlement validates with no Check A/B mismatch (`settlement-validation`). A claim that settles WITH a mismatch is unaffected by the automatic path — the manual-confirm requirement stands exactly as before, since that is precisely the "inconsistent follow-through" case this requirement exists to protect against.

The automatic path SHALL apply regardless of which party (`owed_by`) the outstanding request names — the clean settlement is evidence about the outcome, not about who the app last recorded as responsible for an intermediate step.

#### Scenario: New event arrives on an already-flagged claim
- **WHEN** a claim already in the "needs your action" list (e.g. `suspended`) receives a new event (e.g. `settled`) and that settlement does NOT validate clean
- **THEN** the claim remains visible on the action list, now showing both events, until Justin confirms it resolved

#### Scenario: Justin confirms a claim resolved
- **WHEN** Justin clicks "confirm resolved" on a flagged claim
- **THEN** the claim is removed from the "needs your action" list; this confirmation is itself recorded as an event in the claim's status history

#### Scenario: Claim settles clean with an outstanding info request
- **WHEN** a claim carrying an unresolved `info_requested`/`suspended` event reaches `settled`, and Check A and Check B both find no mismatch
- **THEN** the outstanding event is auto-confirmed via the same path as Justin's explicit tap, and the claim leaves the "needs your action" list without requiring one

#### Scenario: Claim settles with a mismatch
- **WHEN** a claim carrying an unresolved `info_requested`/`suspended` event reaches `settled`, but Check A or Check B flags a mismatch
- **THEN** the outstanding event is NOT auto-confirmed; the claim requires Justin's explicit confirm exactly as before

#### Scenario: Auto-confirm is indifferent to who was owed
- **WHEN** a claim settling clean carries an outstanding request with `owed_by: "vet"`, `"justin"`, or `"petcover"`
- **THEN** the automatic path applies the same way regardless of that value
