# Free LLM providers — survey, 2026-08-02

Research note, not a decision. Nothing here has changed a line of `llm.py`.
Written on `feature/free-llm-survey`, stacked on `feature/integration`.

Prompted by two live failures on 2026-08-02: Groq's daily budget reached
`Limit 100000, Used 96708` from measurement turns alone (task 17.6), and
Gemini's free tier — the only vision backend — exhausted alongside it.

Two subagents surveyed the field. **Every number below marked VERIFIED was
re-checked from this box against the provider's own API after the agents
reported.** The rest is marked and stays marked; this project's rule is that a
plausible assumption is worth nothing.

---

## What was verified first-hand

| Claim | Method | Result |
|---|---|---|
| OpenRouter free-model count | `GET openrouter.ai/api/v1/models`, counted `pricing.prompt == 0 && pricing.completion == 0` | **337 total, 17 free** — not 25 |
| …of which usable here | `supported_parameters` contains `tools`; `architecture.input_modalities` contains `image` | **14 tools, 8 vision, 5 both** |
| Groq has no vision model | `GET api.groq.com/openai/v1/models` with the live key | **15 models, zero vision-capable.** No `scout`, no `maverick`, no `-vl` |
| Groq fallback chain still served | same call | All four of `_FALLBACK_MODELS` present |
| Local inference viability | `nvidia-smi` | **GTX 1650 Ti Max-Q, 4096 MiB VRAM** |
| Gemini free tier trains on input | fetched `ai.google.dev/gemini-api/terms` | **Confirmed verbatim — see below** |

### The finding that matters most

Quoted from Google's own terms, retrieved 2026-08-02:

> **Unpaid Services** … When you use Unpaid Services, including, for example,
> Google AI Studio and the unpaid quota on Gemini API, Google uses the content
> you submit to the Services and any generated responses **to provide, improve,
> and develop Google products and services and machine learning technologies**…
> To help with quality and improve our products, **human reviewers may read,
> annotate, and process your API input and output.** … **Do not submit
> sensitive, confidential** [data]

`extract_vision()` sends scanned vet invoices to the unpaid Gemini quota. Those
carry Justin's name, address, pet names and itemised amounts. Google's own terms
tell us not to send them, and tell us humans may read them.

This is a live boundary problem, not a cost problem. It is the strongest reason
in this document to move `extract_vision` — stronger than the exhausted quota
that prompted the survey.

---

## The real constraint set

Adding an OpenAI-compatible provider is nearly free: `_PROVIDERS` is
`provider -> (base_url, default_model, api_key)`. So "is it free" is not the
filter. Four things are:

1. **Tool calling** — `chat()` runs a ~15-tool loop.
2. **Vision** — `extract_vision()` needs image input.
3. **A genuinely different company** — every entry in `_FALLBACK_MODELS` is a
   Groq model, so one 403 takes chat and extraction down together. Flagged as
   single-point redundancy by the 2026-08-02 eval, axis 5(c).
4. **Does not train on the data** — see above.

### The structural blocker nobody asked about

Both agents converged on this independently, and it is the actual work item:

**`llm.py` cannot express a cross-provider chain today.** `_FALLBACK_MODELS` is
`dict[provider] -> tuple[model]`, and `_client` is a module-global bound to one
`base_url` resolved once from `config.LLM_PROVIDER`. Signing up for a second
provider does not fix constraint 3 — the chain would still walk models within
one provider. A real fix needs chain entries of `(provider, model)` and a
per-provider client cache.

So the provider choice is the smaller half of this. Recorded here rather than
discovered halfway through the change.

---

## OpenRouter

**17 free models, 5 with both tools and vision** (verified):

| id | ctx | note |
|---|---|---|
| `google/gemma-4-31b-it:free` | 262,144 | served by Google AI Studio |
| `google/gemma-4-26b-a4b-it:free` | 262,144 | same |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 256,000 | NVIDIA trains on prompts |
| `nvidia/nemotron-nano-12b-v2-vl:free` | 128,000 | NVIDIA trains; reported degraded |
| `openrouter/free` | 200,000 | picks a random free model per request |

**Limits are requests, not tokens** — 20 RPM always; 50 RPD under $10 lifetime
credits, 1000 RPD at or above it (one-time $10, not a subscription). Documented,
not verified. This trades a token budget for a request budget: a 4-iteration
tool loop burns 4–5 requests per turn, so 50 RPD is roughly ten conversations a
day and 1000 RPD is comfortable.

**Privacy:** logging is opt-in and off by default; of 80 providers exactly two
train — DeepSeek and NVIDIA. Opting out of training costs the NVIDIA models.
The widely-repeated claim that free access requires enabling prompt publication
is **not true of the current roster** — no provider currently has
`canPublish: true`.

**Does it solve the problem?**
- Daily exhaustion — **yes**, no token cap.
- Single-provider fallback — **no.** It replaces four Groq models with one
  OpenRouter account: a new single point.
- Vision — **plausibly**, via `gemma-4-31b-it:free`. OCR quality untested.

One sharp detail: `_completion` only walks the chain when
`_is_daily_budget_exhausted` matches `"tokens per day"` / `"(tpd)"`.
OpenRouter's 429 body carries `rate_limit_exceeded` and never those strings, so
**the chain would silently never walk.** And walking would be wrong anyway — the
RPD cap is account-wide, so switching models frees nothing. The correct
behaviour there is back-off, not fallthrough.

---

## Everything else

| Provider | Free limits | Tools | Vision | Trains? |
|---|---|---|---|---|
| **Groq** (current) | 12K TPM / 100K TPD per model on `llama-3.3-70b`; `llama-3.1-8b-instant` reportedly 500K TPD | yes | **none** (verified) | no-train claimed, unverified |
| **Cerebras** | 30K TPM / 1M TPD per model, docs | yes, incl. parallel | **yes** — `gemma-4-31b` | "zero retention", marketing page only |
| **Google AI Studio** | Flash tier only since Apr 2026 | yes | yes | **yes — verified above** |
| **Mistral** | ~1B tokens/month "Experiment" | yes | Pixtral | trains by default, opt-out |
| **Cloudflare Workers AI** | 10K neurons/day shared across all models | some | `llama-3.2-11b-vision` | unverified |
| **GitHub Models** | 50 RPD high tier | yes | some | unverified |
| **SambaNova** | 20 RPD | undocumented | none | unverified |
| **NVIDIA NIM** | ~40 RPM | yes | yes | warns *do not upload confidential data* |
| **Together AI** | **no free tier** — retired | — | — | — |
| **Hugging Face** | $0.10/month credits | varies | varies | — |
| **Ollama, local** | unbounded | model-dependent | — | perfect |

### Local inference is not the answer on this box

4 GB VRAM (verified) fits roughly a 3–4B model at Q4; an 8B Q4 spills to CPU. A
turn here is ~4.9k tokens against a 15-tool schema and `chat()` runs up to four
rounds. That is squarely the class this project already rejected once — the
`qwen3.6-27b` fallback was excluded at 62 seconds as *"not a fallback, it's a
hang with a reply at the end"*. Local becomes honest at 12–16 GB VRAM.

---

## Shortlist

1. **Cerebras** — the only candidate that answers vision *and* the
   cross-provider gap in one move, at 1M TPD. Blocked on an unknown: this
   account's free tier was sold out on 2026-07-23 and nobody has retested.
2. **Keep Groq primary** — live-verified working today, fastest, and the four
   fallback models are all still served.
3. **OpenRouter as the third leg** — best breadth, real privacy defaults, and
   worth the one-time $10 for 1000 RPD. Not a substitute for a second provider.

## Unverified, and staying that way until someone tests it

- Every rate-limit number in the "everything else" table. Documented only.
- Whether **this account** can get a Cerebras key. This gates recommendation 1.
- Whether tools actually work through OpenRouter's free endpoints with the real
  15-tool schema. No `OPENROUTER_API_KEY` exists in this repo.
- OCR quality of any replacement against `gemini-2.5-flash` on a real scanned
  invoice. This is the one that decides whether the vision move is possible at
  all, and it needs a real PDF, not a benchmark.
- Groq's and Cerebras' training clauses from actual ToS rather than marketing.

## Incidental — offered as hypothesis, not finding

One agent reported Groq returning **403 to python-urllib with its default
User-Agent, and 200 with any UA set**, the only variable changed. That may
explain the unexplained `403 Access denied` recorded at `llm.py:139-142`. Not
reproduced independently. Worth ten minutes before it is believed.

Separately, and confirmed while doing this work: **the host has a TLS intercept
whose CA fails Python 3.13's strict X.509 check** ("Basic Constraints of CA cert
not marked critical"). `certifi` does not contain that CA, so it fails
differently rather than working. Host-side probing of any provider API needs
`ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT` against the Windows store. The
containers are unaffected.
