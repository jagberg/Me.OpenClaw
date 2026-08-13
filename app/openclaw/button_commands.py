"""Every slash command a button is allowed to emit. One declaration, two readers.

**Deliberately dependency-free.** `scripts/gateway_preflight.py` runs at deploy
time, from a worktree that has no virtualenv, so it cannot import anything that
reaches `config` (dotenv) or `db`. Importing `gateway_client` for this tuple
pulled in the whole app; a module with no imports at all does not.

The other reader is `app/gateway-plugin/index.js`, which registers these names
inside the gateway. The two must agree, and the preflight is what proves they
do — a button whose command nobody registered is not an error. It reaches the
agent as a chat turn and spends tokens, measured live on 2026-08-01 when three
`/ping` taps produced three model replies. `/mark 7 sent` arriving at a model as
free text is one typo, one failed plugin load or one rename away.

Card-building code must draw its commands from here, so a new button cannot ship
unasserted.
"""

BUTTON_COMMANDS = (
    "mark",
    "pet",
    "resolve",
    "history",
    "actions",
    "confirm",
    "unmatch",
    "invreq",
    "dismiss",
    "moreinfo",
    "merge",
    "reject",
    "item",
)
