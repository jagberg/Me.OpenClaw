## 1. LLM provider layer

- [x] 1.1 Add `openai` to `app/requirements.txt`
- [x] 1.2 Add config vars to `config.py`: `LLM_PROVIDER` (default `cerebras`), `LLM_MODEL`, `LLM_RATE_LIMIT_PER_MIN`, and per-provider keys (`CEREBRAS_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`); kept existing `GEMINI_*`
- [x] 1.3 Document all new vars in `.env.example` with the Cerebras default and Groq/OpenAI as commented alternatives
- [x] 1.4 Create `app/openclaw/llm.py`: resolve provider → (base_url, model, api_key), one OpenAI-compatible client, reused `_RateLimiter`/`_log_call`, non-silent `LLMUnavailableError`
- [x] 1.5 Implement `chat(messages, tools, tool_impls, ...)` (bounded tool loop, cap 4) and `extract(prompt, purpose)` wrapper
- [x] 1.6 `gemini` selectable via `LLM_PROVIDER=gemini` (extract delegates to `gemini.py`), default off

## 2. Repoint existing extraction sites

- [x] 2.1 `vet_detection.py` → `llm.extract`
- [x] 2.2 `invoice_matching.py` → `llm.extract`
- [x] 2.3 `tasks.py` → `llm.extract` (docstring now names `llm.LLMUnavailableError`)
- [x] 2.4 Repointed `test_core.py` monkeypatches to `llm.extract`; hardened suite to force LLM backends unconfigured. `python tests/test_core.py` → ALL TESTS PASSED (27)

## 3. Agent tools (read + act)

- [x] 3.1 Read tools (compact summaries): `query_claims`, `claim_history` over `vet_claims`/`pets`/`bank_transactions`/`claim_status_events`
- [x] 3.2 Data boundary: explicit safe columns only; no `.env`/secrets/bank fields exposed
- [x] 3.3 Act tools (`propose_mark_sent`/`_set_condition`/`_assign_pet`/`_mark_resolved`) return a proposal descriptor only — no mutation
- [x] 3.4 System prompt: hard rules, identify by pet + Petcover ref, confirm-before-act, ask-when-ambiguous

## 4. Telegram chat wiring

- [x] 4.1 `on_text_reply` routes non-pending text to `agent.handle_message` (via `asyncio.to_thread`)
- [x] 4.2 Proposal rendered as reply + `✅ Confirm` inline button; token `action:claim_id` under the 64-byte limit
- [x] 4.3 `act:` branch in `on_callback` runs the mutation via `claim_forms`/`claim_status` only on tap
- [x] 4.4 Ambiguous/zero-match handled in the tools — agent asks, commits nothing
- [x] 4.5 Existing slash commands / inline flows untouched (chat is additive)

## 5. Verify on real data

- [x] 5.1 Plumbing verified live (config → OpenAI client → request → non-silent error). Cerebras free tier is **sold-out for this account** (402 `payment_required` on *every* free model — gpt-oss-120b, zai-glm-4.7, gemma-4-31b — confirmed via billing page: all inference plans "Temporarily Sold Out"). Switched default to **Groq** (`llama-3.3-70b-versatile`, verified live: `pong`).
- [x] 5.2 Live chat read: "which claims are blocked?" → answered from real DB (22 claims, statuses drafted/matched/pending_match/sent) with no hallucination.
- [x] 5.3 Live act: proposal ambiguity gate holds (multi-claim Aari → agent asks, emits no proposal); `mark_sent` execution flips drafted→sent across the shared `draft_id` group (verified on a DB copy — real DB untouched).
- [x] 5.4 Hard rules in chat: secrets/bank-login query refused; ambiguous act target commits nothing.
- [x] 5.5 `llm_calls` logs both chat + extraction (2 rows, `success=1`); forced provider error surfaces as `LLMUnavailableError` (Cerebras 402 shown earlier).

## 6. Docs

- [x] 6.1 ADR-0009 written, supersedes 0001 (provider abstraction + Cerebras default, Groq/OpenAI fallbacks)
- [x] 6.2 Updated `CLAUDE.md`: LLM provider-agnostic (`llm.py`), Cerebras default; classification/references stay regex/keyword
- [x] 6.3 Live-verified results recorded (5.1–5.5); default provider switched to Groq in `config.py`, ADR-0009 + CLAUDE.md updated.

## 7. Post-deployment hardening (found while running live)

- [x] 7.1 Docker service rebuilt from this branch; runs new code (agent + Groq). Bind-mounted the real data dir (DB + Gmail token) instead of an empty named volume; restored `TELEGRAM_BOT_TOKEN`.
- [x] 7.2 `llm.chat` tool loop: coerce non-dict tool-call args (`"null"`) to `{}` — Groq emitted `arguments: "null"`, crashing `impl(**args)`.
- [x] 7.3 `_handle_chat`: catch-all so any agent error replies in Telegram instead of silent failure (visible-failures rule).
- [x] 7.4 Non-vet denylist as a DB table (`non_vet_merchants`) + `/notvet <merchant>` Telegram command; `vet_detection.classify` checks it before keywords. Seeded `sp vets love pets`; its mis-detected claim removed.
- [x] 7.5 Claim-draft guard: `claim_forms` won't draft a Petcover claim (single or batch) until the itemised invoice is on file — Petcover requires it attached. Flags "awaiting itemised invoice" instead. Reset the one form-only draft (#2).
- [x] 7.6 One-off `scripts/draft_yearly_invoice_requests.py` + `scripts/fill_vet_emails_and_redraft.py`: consolidated past-year invoice requests, one draft per vet, addresses recorded in `vet_contacts` (never guessed).
