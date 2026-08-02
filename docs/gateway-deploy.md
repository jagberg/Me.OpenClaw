# Deploying the two runtimes

`app` owns the claims domain, the database and Gmail. `gateway` owns Telegram,
agent sessions and cron. One command brings up both:

```powershell
./scripts/deploy.ps1              # both runtimes, both versions, preflight
./scripts/deploy.ps1 -SkipTurnCheck   # skips the one agent turn the preflight spends
```

A partial start is a **failure**, not a success — one runtime up and the other
down is the state that looks fine and silently does half the job.

## Before the first deploy

Copy `.env.example` to `.env` **in the worktree you deploy from** and fill in
three values. This is a different file from `app/.env`, deliberately:

- Compose variable interpolation reads the root `.env` or the shell. `env_file:`
  only populates a container's environment; it does not feed `${...}`.
- Pointing the gateway at `app/.env` would hand it the Gmail credential and
  `DATABASE_PATH`, which is exactly what the isolation boundary forbids
  (ADR-0024). Keeping the files apart means nobody can do that by accident
  without noticing they are duplicating values.

`INTERNAL_API_SECRET` must be **identical** in both files. That is a divergence
hazard — this repo already has one, since `app/.env` differs between the main
checkout and the deploy worktree. It fails loudly rather than quietly: if they
disagree, the plugin's boot report is rejected, `/health.gateway_plugin` stays
empty, and the preflight fails the deploy naming it.

## What the preflight asserts

`scripts/gateway_preflight.py`, run by `deploy.ps1`. Every check exists because
something was found **silently** wrong during the 2026-08-01 spike. All of it is
configuration, so no test in `app/tests/test_core.py` can see it.

| Check | Guards against |
|---|---|
| app reachable | the two runtimes started but cannot talk |
| boundary plugins disabled | 47 of 66 plugins are on by default, every boundary-relevant one among them; an upgrade re-enables them with no signal |
| access policy | the `dmPolicy` default hands an unknown sender a live pairing code and the command to request approval |
| media outbox narrow | `media.localRoots: "any"` is one word and disables the whole media control |
| gmail-isolation-boundary | a Gmail credential, a Google key or a mount reaching `app/data` on the gateway |
| gateway menu scopes unchanged | the app owns Telegram's per-chat command scope only while the gateway leaves it alone |
| button commands registered | an unregistered command in a button is not an error — it reaches the model and spends tokens |
| model serves a turn | a model id config *accepts* can still fail at runtime with `model_not_found` |
| turn size under ceiling | itemised, not a total: a component regressing under a passing total is the failure this catches |

A check that cannot run reports **SKIP**, and the run says plainly that skips are
gaps rather than passes.

## What CANNOT be asserted, and why

A checklist that quietly omits an unverifiable item reads as full coverage. This
section is the honest remainder. Nothing below is covered by the preflight or by
the smoke suite, and each one has been observed at least once.

**A button command cannot be invoked by the deploy.** There is no
`openclaw command run` — the only real dispatch path is a Telegram tap, and a
deploy script must not fake one against Justin's chat. The preflight asserts
that the plugin *reported* registering each command; a real tap on 2026-08-02
proved the chain end to end, but it is a manual step and always will be.

**A plugin cannot tell whether it owns the names it registered.**
`registerCommand` neither throws nor returns a failure when the name is taken;
the gateway logs it asynchronously about a second later. The preflight reads the
gateway's log for those lines, which means the check depends on the log file
existing and keeping its format. Two consequences: after a log rotation the
window is gone, and a format change turns the check into a silent pass. It has
already been silently wrong once — the log is JSON, and a pattern written for
plain text matched nothing while three real collisions sat in the file.

**Nothing can confirm a message rendered the way it was sent.** The send API
returns `ok: true` with a real message id for a payload the platform then
discards, and there is no read-back. The worst case is *partial*: a valid
presentation renders its buttons and silently drops its text blocks, so the
message arrives looking deliberate. `gateway_client` refuses the payload shapes
known to be dropped; it cannot see the ones nobody has hit yet.

**A caption edit that failed looks like one that succeeded.** The CLI exposes no
`--caption` flag, so every media edit takes `editMode: "auto"`, which writes
`editMessage failed: ... there is no text in the message to edit` into the
gateway's log **on success**. Since the claim cards are images, that is the
normal path for every tap result, and a genuine failure is indistinguishable in
that log.

**Prompt-level rules are not enforceable and must not be relied on.** Twice
observed: the stock agent asserted it had checked a mailbox in a runtime holding
no mail credential, and our own MCP instructions demanded a claim `#id` in every
answer and the first live turn omitted them. Anything that must hold lives in
code — the harness refusals, the proposal gate, the no-send rule.

**A Google key cannot be shown to lack a Gmail scope from outside.** The
preflight asserts the *absence* of any Google or Gemini credential on the
gateway, which is stronger and simpler. The agent runs on Groq, so there is no
legitimate reason for one to be there.

**Runtime media roots are read from config, not from the running process.** If
the two ever diverge the check reads the intention rather than the fact.

**Nothing here covers real load.** Behaviour under Justin's actual message
volume, concurrent taps, or a gateway restart mid-handler is unverified. The
replay guarantees (ADR-0014) are carried forward on the strength of the existing
implementation, not re-proved against the new transport.

## Operational notes

- **The gateway's log lives inside its container** (`/tmp/openclaw/*.log`) and is
  lost on recreate. It is outside every backup, and the collision check above
  depends on it.
- **`gateway_state` is a named volume outside `db_backup`'s scope.** Losing it
  costs the agent's session history, not claim state.
- **Never diagnose a plugin from `plugins list`.** Those fields come from a
  persisted registry that goes stale silently and has reported `commands: []`
  for commands that demonstrably worked. Test the behaviour.
- **`APP_VERSION` and `GATEWAY_VERSION` are separate and must stay separate.**
  `telegram_messages.app_version` exists so the message log is a dataset keyed to
  the code that produced each row; two runtimes mean two versions, and
  conflating them makes the dataset lie.
