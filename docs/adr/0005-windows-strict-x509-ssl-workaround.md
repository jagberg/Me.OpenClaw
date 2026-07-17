# ADR-0005: Windows strict-X.509 SSL workaround for outbound HTTPS

**Date**: 2026-07-08
**Status**: accepted
**Deciders**: Justin (implementation-level, discovered during live testing)

## Context

On Justin's dev machine, all outbound HTTPS from Python (`requests`/`urllib3`-based clients — Gemini SDK, Gmail client) failed with `CERTIFICATE_VERIFY_FAILED: Basic Constraints of CA cert not marked critical`. Root cause: Python 3.13's OpenSSL 3 context enables strict X.509 validation by default, and a locally-installed root CA (from endpoint security/EDR software) doesn't mark its Basic Constraints critical — a real non-conformance in that cert, tolerated by Windows/browsers but rejected by strict OpenSSL.

## Decision

Patch `ssl.SSLContext.wrap_socket` at process startup to clear `ssl.VERIFY_X509_STRICT` before every handshake. Full chain and hostname verification stay on — only that one flag is relaxed. Implemented in `app/openclaw/ssl_compat.py`, called from `gemini.py` and `gmail_ingest.py`.

## Alternatives Considered

### Alternative 1: Inject a custom SSLContext via `requests`' `HTTPAdapter`/`PoolManager`
- **Pros**: would be the "proper" documented extension point
- **Cons**: verified empirically that `requests`/`urllib3` silently discard or rebuild any injected context several layers deep in their connection-pool plumbing — does not reliably work
- **Why not**: doesn't actually fix the problem after extensive tracing

### Alternative 2: `pip-system-certs` package
- **Pros**: popular, well-known package for this class of problem
- **Cons**: only patches `requests`' adapter layer (same broken path above) and never touches the strict-validation flag at all
- **Why not**: tested directly, confirmed non-functional for this specific failure

## Consequences

### Positive
- One small, well-understood patch fixes HTTPS for every library in the process
- Documented as a learned skill (`~/.claude/skills/learned/windows-ssl-strict-x509-workaround.md`) so future sessions on this machine don't re-debug it

### Negative
- Relaxes a real (if narrow) security check process-wide, not just for the one known-bad CA
- Machine-specific workaround baked into project code — will look mysterious to anyone running this on a machine without the same EDR software

### Risks
- If the local EDR's root CA is ever compromised or replaced with something worse, this flag relaxation makes that slightly easier to miss — accepted given it's still constrained to full chain+hostname verification, not a blanket cert-check bypass
