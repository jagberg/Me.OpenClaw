## ADDED Requirements

### Requirement: Petcover's claims mail is never consumed by task capture

The system SHALL exclude every sender in `PETCOVER_STATUS_SENDERS` from task capture, and SHALL
leave those messages **unmarked** in `processed_emails` so the claims poller still finds them.
Marking without tasking is not sufficient: the mark is the lockout.

`processed_emails` is a single dedupe gate shared by two pollers that scan overlapping mail:
`gmail_ingest.poll_once` (inbox → candidate tasks) marks every message it sees, and
`pipeline.poll_petcover_status` skips any message already marked. Whichever poller runs first
therefore wins, permanently — the loser never sees the message at all.

Verified live 2026-08-04. Five `Claim Approval` letters between 28/07 and 03/08 lost the race, carry
a `processed_emails.task_id` and produced **no claim status event of any kind**, while the five
letters that reached the claims service all carry `task_id: NULL`:

| letter | date | reference | claimed | paid | outcome |
|---|---|---|---|---|---|
| `19fa67bc840eaf82` | 28/07 | `DC1-27-5628` Tr 7 | $132.50 | $86.13 | task 139, no event |
| `19fb4d361c76f24d` | 31/07 | `DC1-26-5993` Tr 1 | $944.50 | $516.42 | task 152, no event |
| `19fc4f93cd570a7b` | 03/08 | `DC1-27-5628` Tr 8 | $580.74 | $289.73 | task 161, no event |
| `19fc4ff987644163` | 03/08 | `DC1-26-5992` Tr 4 | $135.00 | $87.75 | task 162, no event |
| `19fc4ff8acc16ada` | 03/08 | `DC1-26-5992` Tr 3 | $2,521.46 | $1,638.95 | task 163, no event |

Nothing else was going to stop them: `claims.au@petcovergroup.com` matches neither `_is_noise`
branch — the local-part is not in `_AUTOMATED_SENDER` and the letters carry no `List-Unsubscribe` —
so every one was classified a genuine human reply and turned into a task. The latest `approved`
event in the log is #55 (2026-07-30) against a live mailbox holding ten approval letters.

This is not a routing failure and not a claim-matching failure: claim #13 carries
`DC1-27-5628 sr 7`, exactly the serial the 28/07 letter cites. The letter never arrived.

Recovery is the existing re-read (`poll_petcover_status(reread=True, since=…)`), which ignores the
gate and relies on `process_reply`'s per-(email, claim, event) idempotency, so it records only what
is genuinely new and cannot resurrect a dismissed settlement difference. No repair script and no
backfill.

#### Scenario: A Petcover claims letter arrives
- **WHEN** `gmail_ingest.poll_once` sees a message from a sender in `PETCOVER_STATUS_SENDERS`
- **THEN** no task is created, no `processed_emails` row is written for it, and the claims poller subsequently processes it

#### Scenario: Ordinary mail is unaffected
- **WHEN** the message is from a vet clinic, or any sender outside that list
- **THEN** task capture behaves exactly as before, including its noise filtering

#### Scenario: A Petcover letter already locked out
- **WHEN** a letter carries a `processed_emails` row from the task ingest and no claim status event
- **THEN** a re-read with `reread=True` reaches it, and records only events not already logged
