"""One vocabulary for vet names, the way `status_labels` is one for statuses.

A NetBank descriptor is not a name. It is SHOUTY, carries a suburb and a state,
sometimes an ABN-era legal entity, and occasionally names a *different suburb
from the practice Justin knows* — `BANKSTOWN VET PEAKHURST NSW` is the clinic he
calls Boundary Road Vet. Rendered raw it is both too long for a card row and,
worse, ambiguous about who was actually seen.

**Why a hand-written map rather than a clever shortener.** A heuristic that
strips suburbs and states would tidy the length and do nothing for the ambiguity,
which is the half that matters — no amount of string processing turns
"BANKSTOWN VET PEAKHURST" into "Boundary Road Vet". And a wrong vet name on a
claim card is not cosmetic: it is the line Justin reads to decide which
invoice he is looking at. So the mapping is data he can check, versioned in the
repo, reviewable in a diff.

**Match on a substring of the descriptor, not equality.** Bank descriptors drift
— a terminal is replaced, a suburb is added, the entity name changes — and the
project has been bitten by exactly that kind of silent drift before. A
substring keeps working when the tail moves.

Unknown merchants fall through to the old behaviour: title-case the shouty ones,
leave mixed-case styling alone, clip. A vet with no alias is never *wrong*, only
long, so an unmapped one degrades quietly rather than guessing.
"""

# Matched case-insensitively against the descriptor, first hit wins. Keep the
# keys distinctive enough that a new clinic cannot collide with an old one.
#
# CONFIRM WITH JUSTIN BEFORE TRUSTING TWO OF THESE (2026-08-02):
#   - "Boundary Rd Vet" is his own correction of BANKSTOWN VET PEAKHURST. The
#     direction is assumed: he named the practice, so the practice name is what
#     shows. If he would rather see the descriptor's own wording, flip it.
#   - "SAH Inner West" leaves SAH unexpanded because nobody has said what it
#     stands for. Guessing would breach the never-guess rule on the one field
#     this module exists to make trustworthy.
ALIASES = {
    "bankstown vet": "Boundary Rd Vet",
    "the shire veterinary": "The Shire Vet",
    "kings vet": "Kings Vet",
    "medipaws": "MediPaws Leichhardt",
    "sah inner west": "SAH Inner West",
}


def display(merchant: str | None, limit: int | None = None) -> str:
    """The name a human should read for this bank descriptor.

    `limit` clips only when the caller has a width to respect; the dashboard and
    the chat agent have none and pass nothing.
    """
    raw = (merchant or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    for needle, name in ALIASES.items():
        if needle in lowered:
            return name
    # No alias: the pre-existing behaviour, unchanged. Title-case only the
    # all-caps descriptors — one that already has mixed case ("MediPaws Sydney")
    # is the vet's own styling and .title() would flatten it.
    tidied = raw.title() if raw.isupper() else raw
    if limit is not None and len(tidied) > limit:
        return tidied[: limit - 1].rstrip() + "…"
    return tidied
