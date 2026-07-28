"""The one place a claim's state is put into words.

`vet_claims.status` is pipeline state; what Justin reads is a separate thing.
They used to be the same string, copied into three hand-synced maps
(`claim_card`, `templates/index.html`, `templates/basic.html`) plus pipeline's
notify text — so `matched` read identically whether a claim needed one field
typed in or could never proceed at all, and an information request read
"Suspended" because that is the status the letter happened to set.

Add a state's wording here and every surface picks it up. Colours and severity
classes stay with their renderers but key off the **status**, so a rewording
can never silently drop a colour.
"""

from .claim_status import _ACTION_META, _action_kind_from_row

LABELS = {
    "pending_match": "No invoice",
    "matched": "Matched",
    "drafted": "Drafted",
    "sent": "Sent",
    "acknowledged": "Acknowledged",
    "info_requested": "Info requested",
    "suspended": "Suspended",
    "approved": "Approved",
    "settled": "Settled",
    "declined": "Declined",
    "below_excess": "Below excess",
    "absorbed": "Absorbed",
}

# A `matched` claim spans "needs one field from you" and "can never proceed".
# The determination is `claim_status`'s, not a second copy of it.
_MATCHED_LABELS = {
    "set_condition": "Needs condition",
    "assign_pet": "Needs pet",
    "blocked_insurer": "Blocked: no claim process",
}

# Petcover asks the vet for the document as often as it asks Justin, and he is
# only Cc'd on the vet's copy. `claim_status.resolve_owed_by` records which;
# unrecorded stays neutral rather than guessing, because naming the wrong party
# is how a claim quietly dies waiting for someone who was never asked.
_INFO_REQUEST_LABELS = {
    "vet": "More vet info required",
    "justin": "Petcover needs info from you",
}


def label(claim, owed_by: str | None = None) -> str:
    """Display wording for one claim row (`sqlite3.Row` or the ledger's dict).

    `owed_by` comes from the latest `info_requested` event's detail; callers
    that don't have it (or rows that carry it themselves) can leave it out.
    """
    status = claim["status"]
    if status == "matched":
        return _MATCHED_LABELS.get(_action_kind_from_row(claim), LABELS["matched"])
    if status == "info_requested":
        owed = owed_by or _get(claim, "owed_by")
        return _INFO_REQUEST_LABELS.get(owed, LABELS["info_requested"])
    return LABELS.get(status, status)


# /basic answers a different question from a table chip — "what's needed", not
# "where is this". Same module, so there is still one place wording lives; the
# action half reuses `_ACTION_META`'s titles rather than restating them.
_WAITING = {
    "pending_match": "Upload invoice",
    "sent": "Awaiting Petcover",
    "acknowledged": "Awaiting Petcover",
    "approved": "Awaiting settlement",
    "suspended": "Claim suspended",
}
_INFO_REQUEST_NEEDS = {
    "vet": "Chase vet for the info",
    "justin": "Petcover needs info from you",
}


def needs(claim, owed_by: str | None = None) -> str:
    """The one-line "what's needed" phrase for the phone-first view."""
    kind = _action_kind_from_row(claim)
    if kind:
        return _ACTION_META[kind][0]
    status = claim["status"]
    if status == "info_requested":
        owed = owed_by or _get(claim, "owed_by")
        return _INFO_REQUEST_NEEDS.get(owed, "Petcover needs info")
    return _WAITING.get(status) or label(claim, owed_by)


def _get(claim, key):
    try:
        return claim[key]
    except (KeyError, IndexError):
        return None
