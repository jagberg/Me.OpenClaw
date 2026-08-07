# ADR 0028: Groq's 403 was a User-Agent ban, not a network block

- Status: accepted
- Date: 2026-08-08
- Deciders: Justin
- Supersedes the *conclusion* of ADR-0009's 2026-08-04 amendment. That
  amendment's **Decision** — Gemini as the default `LLM_PROVIDER`, Groq left
  configured — stands unchanged and is not revisited here. What is superseded is
  its finding of fact: "Groq is refusing this network. Nothing in this repo can
  fix it."

## Context

On 2026-08-04 every probe of `api.groq.com` returned `403`, and the amendment to
ADR-0009 recorded it as a network-level refusal: the same 403 with no
`Authorization` header, with a valid key, with a garbage key, from the Windows
host, from the app container and from the gateway container. The conclusion
drawn was that nothing here could fix it.

That conclusion is the expensive part, not the 403. It closed the question. For
the four days it stood, the LLM backend had exactly one reachable provider, and
ADR-0017's fallback chain — four links, all Gemini models after the swap — had
no cross-provider option to reach for. On 2026-08-06 the gateway heartbeat spent
Gemini's entire free-tier daily quota on scheduled agent turns; taps kept working
because they never reach a model, and every **typed** message answered "API rate
limit reached" with nowhere to fall through to.

What nobody had done in those four days was probe with the client the code
actually runs.

## Decision

Groq is treated as **reachable**, and the 403 is understood as a Cloudflare
User-Agent ban on the *probe*, not a block on this network.

Measured 2026-08-07 from inside the running containers:

| client | container | result |
|---|---|---|
| `openai` SDK (httpx 0.28.1), default UA | app | **200**, real completion from `llama-3.3-70b-versatile` |
| Node `fetch`, default UA | gateway | **200**, real completion |
| `urllib`, default UA | app | **403**, body `error code: 1010` |
| `urllib`, UA `curl/8.0` or `Mozilla/5.0` | app | **401 Invalid API Key** |

Cloudflare's `1010` is a User-Agent ban. Setting any UA turns the 403 into a
`401` — which is Groq answering, i.e. the request arrived. Both runtimes reach
Groq with their own default clients and need no workaround: `llm.py` goes through
the `openai` SDK, and the gateway goes through Node `fetch`. Neither has ever
been subject to the filter that every recorded probe measured.

Two rules follow, and the second is the general one:

1. **`urllib` is not a valid probe for this API.** Its default User-Agent is
   banned, so it reports a failure the application does not experience.
2. **Probe with the client the code uses.** This is the repo's existing "a silent
   result is not a finding" rule (root `CLAUDE.md`) meeting its converse: a
   *loud* result from the wrong client is not a finding either.

**No configuration is changed by this ADR.** `LLM_PROVIDER` remains `gemini` and
the gateway agent's primary remains `gemini/gemini-2.5-flash`. Moving a primary
provider is a behaviour change and Justin's call; this ADR records a fact, and
deliberately does not smuggle a config change in behind it.

## Consequences

**The cross-provider fallback is now buildable, and needs no new account.** This
is the point. ADR-0026 and `openspec/BACKLOG.md` both ask for a chain that
survives a provider-level failure, and both were gated on finding a second
reachable provider. There already was one. Gemini's daily exhaustion — the live
failure of 2026-08-06 — now has somewhere to go.

**ADR-0017's defect is unchanged.** Its chain is per-*model* insurance and every
link is one provider; that is still true and still the standing gap in
`docs/failure-modes.md`. What changed is that a second provider is available to
build the per-provider half against, not that the chain got better on its own.

**A limitation, recorded rather than resolved: it is undetermined whether
2026-08-04's block was this same artefact.** The body recorded then —
`{"error":{"message":"Access denied. Please check your network settings."}}` —
is a *different string* from today's `error code: 1010`. A genuine block that has
since lifted is equally consistent with both observations, and nothing available
now can distinguish the two retroactively. The amendment's measurement is not
being called wrong; only its conclusion is, and only as a statement about the
present. Anyone re-reading this should re-measure rather than trust either date.

**Groq's ceiling still applies.** 100k tokens/day, per model — not a context cap
(that is 131k). Reachability is not capacity, and a Groq link in a fallback chain
buys another daily budget, not an unmetered one.

**This does not unblock ADR-0026's vision question.** Verified 2026-08-02 and
unchanged: Groq serves no vision model at all, so `extract_vision()` has no
in-provider alternative and the privacy exposure recorded against the unpaid
Gemini quota stays exactly where it was. ADR-0026 remains **proposed**.
