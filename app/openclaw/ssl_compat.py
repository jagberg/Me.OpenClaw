"""Works around a local TLS trust issue seen on this machine: a security/EDR
tool injects a root CA into the Windows trust store whose Basic Constraints
extension isn't marked critical (a spec violation the CA issuer made, not us).
Python 3.11+'s OpenSSL 3 context enables strict X.509 validation by default and
rejects it outright.

A second, distinct problem shows up for some hosts (seen on oauth2.googleapis.com
during the Gmail OAuth token exchange): `requests`/urllib3 build their SSLContext
from certifi's bundled CA file, which never had the corporate root CA injected
into it at all (only Windows' native store has it) — that fails with "unable to
get local issuer certificate", a missing-CA error the strict-flag clear alone
can't fix since the chain can't be built at all, not just rejected for being
non-conformant.

`requests`/`urllib3` build their own SSLContext internally per-request and
don't reliably honor one injected via the adapter/pool-manager layer (verified
empirically — an injected context is silently dropped several layers deep in
urllib3's connection-pool plumbing). Sync handshakes all go through
`SSLContext.wrap_socket`; async ones (httpx/anyio, e.g. python-telegram-bot)
go through `SSLContext.wrap_bio` instead — memory BIOs, no socket — so both
choke points get the same treatment right before the handshake: the OS trust
store is merged in (`load_default_certs`, additive to whatever certifi certs
are already loaded) and VERIFY_X509_STRICT is cleared — full chain and
hostname verification stay on in both cases, only the missing local CA and
the one strict flag are added back / relaxed.
"""

import ssl

_patched = False


def patch_requests_to_use_os_trust_store() -> None:
    global _patched
    if _patched:
        return

    original_wrap_socket = ssl.SSLContext.wrap_socket
    original_wrap_bio = ssl.SSLContext.wrap_bio

    def _fix(context: ssl.SSLContext) -> None:
        context.load_default_certs()
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    def wrap_socket(self, *args, **kwargs):
        _fix(self)
        return original_wrap_socket(self, *args, **kwargs)

    def wrap_bio(self, *args, **kwargs):
        _fix(self)
        return original_wrap_bio(self, *args, **kwargs)

    ssl.SSLContext.wrap_socket = wrap_socket
    ssl.SSLContext.wrap_bio = wrap_bio
    _patched = True
