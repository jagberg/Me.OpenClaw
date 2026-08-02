# ADR 0023: The agent's tool allowlist serves security and feasibility at once

- Status: accepted
- Date: 2026-08-01
- Related: ADR-0009 (LLM backends), ADR-0016 (agent boundaries), ADR-0017 (per-model daily budget)

## Context

Moving the conversational agent onto the OpenClaw gateway puts a general-purpose
agent runtime in the loop. Two separate requirements landed on the same lever,
and only one of them was anticipated.

**The security requirement, anticipated.** ADR-0016's boundaries were held by a
tool registry in `agent.py` plus prompt discipline. A general-purpose runtime
ships `exec`, `read`, `edit`, `process`, `web_search` and a browser by default.
Prompt discipline is not a boundary against those; an absent tool is. The
gateway's stock inventory was measured at 32 tools, 31,972 chars of schema.

**The feasibility requirement, not anticipated.** Tool schemas are transmitted on
every request. A stock turn measured 22,810 prompt tokens for the message `hi`,
against Groq free tier's ceiling of 12,000 tokens per minute. This is a
per-request impossibility, not exhaustion — the same request fails no matter how
long you wait. Groq is the provider ADR-0009 standardises on and ADR-0017's
daily-budget chain assumes.

For most of 2026-08-01 the second requirement was recorded as unsolvable. Two
successive measurements said so: first "one turn is ~2x the ceiling, a hard
blocker", then "the floor is ~29k tokens and plugin pruning cannot move it".
Both read a single total off Groq's `413 Request too large ... Requested N`
error. Neither measured what the total was made of.

`openclaw agent --json` returns a `systemPromptReport` itemising every
contributor. Itemised, three-quarters of the turn was content the deployment
chooses.

## Decision

Restrict the agent's tools with the gateway's top-level `tools.allow`, and treat
that allowlist as load-bearing for **both** requirements simultaneously.

Ship the agent's workspace markdown files from the repository rather than
accepting the seeded templates, for the same dual reason: they are prompt
content that shapes behaviour, and they are 14,341 chars on every turn.

Measured result:

| Configuration | Prompt tokens | Tool schemas | Workspace files |
|---|---:|---:|---:|
| Stock | 22,810 | 31,972 (32 tools) | 14,341 (7 files) |
| Authored workspace files | 20,616 | 31,972 | 6,508 (6 files) |
| \+ tool allowlist | **5,355** | 304 (1 tool) | 6,508 |

Groq therefore remains the provider for the agent, consistent with ADR-0009,
with roughly 6,600 tokens of headroom for the entire claims tool inventory.

Config detail worth recording because two wrong guesses preceded it: the key is
top-level **`tools.allow`**. `agents.defaults.tools.allow` is rejected with
`Unrecognized key: "tools"`, and `agents.defaults.contextPruning.tools.allow` —
inferred from a docs search result — does not exist. `alsoAllow` adds to the
active profile; `allow` replaces it.

## Consequences

**The two requirements can now break each other silently.** Someone adding a
tool for a legitimate feature reduces the security boundary *and* spends the
token budget, and neither shows up as an error. Someone trimming tools to save
tokens may remove something the claims flow needs. The deploy preflight
therefore asserts both properties independently rather than assuming one implies
the other.

**The tool budget is a derived number, not a preference.** ~6,600 tokens is the
provider ceiling less everything else in a turn. For scale, the platform's own
32-tool inventory was about five times that, so an inventory that looks
reasonable can breach it. `claims-mcp-surface` fails the suite on the tool that
crosses the line.

**The floor is the core system prompt**, 18,536 chars / ~4.6k tokens, which no
configuration here moves. Any future budget sits on top of it.

**Limitations, recorded rather than discovered later:**

- The 5,355 figure was measured with `tools.allow = ["read"]` as a one-tool
  stand-in. `read` is a filesystem tool this ADR's security half forbids; the
  shipped allowlist is the claims MCP tools. The shape of the result holds, the
  exact number will move.
- No claims tools existed at measurement time. The headroom is what they must
  fit inside, not spare capacity.
- 13 irrelevant skills still contribute 4,206 chars (`meme-maker`, `weather`,
  `notion`, `clawhub`). Roughly another 1k tokens is available and untaken.
- Groq's per-minute limit is distinct from ADR-0017's per-day, per-model budget.
  Both apply. Clearing the first does nothing about the second.

**Method note, which is the durable part.** Two wrong conclusions came from
reading a total off a provider's error message. A total tells you that you are
over a limit; it never tells you what to cut, and it invites the inference that
nothing can be cut. Measure composition before concluding a limit is structural.
