## ADDED Requirements

### Requirement: Host-side access to the live DB is mechanically enforced, not conventional

ADR-0018 requires every host-side connection to the live SQLite DB to be `file:…?mode=ro`. That requirement SHALL be enforced by a mechanism, not by convention alone. A host-side command that opens the live DB path without `mode=ro` SHALL be rejected before it runs.

The ADR's own words are the justification: *"Nothing prevents the next plain `connect()`."* Convention has now failed twice — four read-write opens in one session on 2026-07-27, and the rule was in `CLAUDE.md` and the ADR was read later that same session. A plain `connect()` checkpoints and deletes `openclaw.db-wal` / `-shm` on close, which took the container down for 51 minutes on 2026-07-25.

A read-only helper alone SHALL NOT be considered sufficient. The ADR raises the objection itself: an ad-hoc one-liner bypasses a helper, and the observed failure mode is exactly an inline `sqlite3.connect(<live path>)`.

#### Scenario: A host-side command opens the live DB read-write

- **WHEN** a command run from the host references the live DB path without `mode=ro`
- **THEN** it SHALL be rejected before execution, naming the read-only form to use

#### Scenario: A host-side command opens the live DB read-only

- **WHEN** a command uses `file:…?mode=ro` with `uri=True`
- **THEN** it SHALL run unimpeded

#### Scenario: A write to the live DB is genuinely needed

- **WHEN** a deliberate write to the live DB is required
- **THEN** it SHALL run inside the app container, with a backup taken and a dry-run diff reviewed first
- **AND** the guard SHALL NOT be disabled to allow a host-side write

### Requirement: The phantom DB fails loudly rather than answering

A host-side call that would open the phantom DB SHALL raise rather than return rows, because a wrong answer reported as fact is worse than an outage.

`app/.env` sets `DATABASE_PATH=/data/openclaw.db` — a container path — and `config` loads `.env` from cwd, so a host-side `db.get_connection()` silently resolves to `C:\data\openclaw.db`, a stale file that returns plausible wrong rows instead of an error. This is worse than the failure ADR-0018 guards against: a read-write open of the *live* DB breaks loudly, while this breaks quietly.

#### Scenario: App code is run from the host against the live path

- **WHEN** `db.get_connection()` is called from the host and resolves outside the intended data directory
- **THEN** it SHALL raise, naming the phantom and the two supported alternatives (a read-only copy, or running inside the container)
