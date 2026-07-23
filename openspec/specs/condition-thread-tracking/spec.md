# condition-thread-tracking Specification

## Purpose
Model a Petcover claim reference as a **Condition Thread** — one (pet, condition) pairing whose reference is reused for the life of the condition — and route Petcover's reply events to the correct claim within a thread using the reference and Petcover's per-document serial, never disturbing terminal (settled/declined) claims and never letting one declined thread block another.

## Requirements

### Requirement: A claim belongs to a Condition Thread with a per-document serial
A Petcover reference identifies a Condition Thread — one (pet, condition) pairing reused for the life of the condition (proven: settled and reused months apart). The system SHALL store, per claim, the thread's reference (`petcover_reference`) and Petcover's document serial (`petcover_sr`) learned from their letters ("DC1-27-5628 Sr 3").

#### Scenario: Acknowledgement carries reference and serial
- **WHEN** an acknowledgement correlates to a claim and contains reference `DC1-27-5628` and `Sr 3`
- **THEN** the claim stores reference `DC1-27-5628` and `petcover_sr = 3`

#### Scenario: Reference reused months after settlement
- **WHEN** a new acknowledgement arrives carrying a reference that only settled claims currently hold
- **THEN** the new claim joins the thread with its own Sr, and no settled claim's status or events are touched

### Requirement: Events route by reference and serial, never to terminal claims
The system SHALL route a classified Petcover event: (1) to the single claim matching (reference, Sr) when the letter cites a serial; (2) when the letter cites only a reference, to that thread's non-terminal claims only — claims whose status is `settled` or `declined` SHALL never receive routed events.

#### Scenario: Letter cites reference and serial
- **WHEN** a suspension letter cites "DC1-27-5628 SR1" and a claim holds that (reference, sr)
- **THEN** the event attaches to that claim alone

#### Scenario: Reference-only letter with settled siblings
- **WHEN** an info-request cites only `DC1-27-5628`, and the thread holds two settled claims and three acknowledged claims
- **THEN** the event attaches to the three acknowledged claims only

### Requirement: A declined thread never blocks other threads
Decline events SHALL be terminal only for the claims of their own thread. Claims in other threads — including threads fed by the same Submission — SHALL proceed unaffected.

#### Scenario: One of a submission's two threads is declined
- **WHEN** a submission's invoices were filed by Petcover into two threads and one thread receives a decline
- **THEN** only that thread's claims become `declined`; the other thread's claims keep their status and continue receiving events
