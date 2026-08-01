"""Renders the /history claim card as a PNG.

Telegram can't be trusted to render an aligned table in text — its default
font is proportional and the client reflows on narrow screens. A rendered
image is the only way to get real columns, colour-coded status chips and
month grouping to look the same on every device.

Supersampled 2x then downscaled, which is what keeps the small text and the
chip corners from looking ragged.
"""

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from . import status_labels

S = 2  # supersample factor — draw at 2x, downscale on save
W = 560
ROWS_PER_PAGE = 12

# Font files by role. DejaVu is the Docker path (installed via the Dockerfile);
# the Windows path keeps local runs and tests working. Falling back to
# load_default means a missing font degrades the look, never crashes the card.
_FONT_CANDIDATES = {
    "sans": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"],
    "sans_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf"],
    "mono_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "C:/Windows/Fonts/consolab.ttf"],
}


def _font(role: str, size: int):
    for path in _FONT_CANDIDATES[role]:
        try:
            return ImageFont.truetype(path, size * S)
        except OSError:
            continue
    return ImageFont.load_default(size * S)


BG = (23, 26, 33)
CARD = (30, 34, 43)
LINE = (48, 54, 66)
TXT = (233, 237, 244)
DIM = (140, 149, 165)
FAINT = (98, 106, 122)
OK = (86, 208, 122)

# (dot/text colour, chip background) per **status** — keyed by the stored value,
# not by the wording, so rewording a label in status_labels can never silently
# drop a colour back to the default. The words live there; only colour is here.
_STATUS_COLOURS = {
    "pending_match": ((255, 176, 66), (58, 42, 20)),
    "matched": ((94, 214, 195), (20, 48, 46)),
    "drafted": ((139, 156, 212), (27, 33, 51)),
    "sent": ((122, 168, 255), (26, 38, 62)),
    "acknowledged": ((177, 148, 255), (40, 34, 64)),
    "info_requested": ((228, 168, 78), (46, 36, 20)),
    "suspended": ((232, 138, 96), (48, 30, 22)),
    "approved": ((110, 214, 168), (18, 46, 38)),
    "settled": (OK, (22, 48, 32)),
    "declined": ((224, 108, 96), (48, 23, 21)),
    "below_excess": ((150, 158, 175), (44, 48, 58)),
    "absorbed": ((120, 128, 145), (38, 42, 52)),
}

PAD = 22
MARGIN = 10
ROW_H = 46
MONTH_H = 30
GROUP_GAP = 8
HEADER_H = 30 + 26 + 18
FOOTER_H = 32


def _rrect(d: ImageDraw.ImageDraw, box, radius: float, fill) -> None:
    """All geometry is written in final pixels and scaled to the supersampled
    canvas here, so layout code never carries S around."""
    d.rounded_rectangle([c * S for c in box], radius=radius * S, fill=fill)


def _text(d: ImageDraw.ImageDraw, xy, s: str, font, fill, anchor: str = "la", spacing: float = 0) -> None:
    if not spacing:
        d.text((xy[0] * S, xy[1] * S), s, font=font, fill=fill, anchor=anchor)
        return
    x, y = xy[0] * S, xy[1] * S
    for ch in s:  # per-char draw: PIL has no letter-spacing option
        d.text((x, y), ch, font=font, fill=fill, anchor=anchor)
        x += d.textlength(ch, font=font) + spacing * S


def _status_label(status: str) -> str:
    return status_labels.LABELS.get(status, status.replace("_", " ").capitalize())


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _vet_name(merchant: str) -> str:
    """Delegates to the one vet vocabulary — see `vet_names.py`.

    Kept as a name here because `telegram_bot` already calls it, so both the
    cards and the text messages pick up an alias from one edit. Length was never
    the interesting problem: `BANKSTOWN VET PEAKHURST NSW` fits, and still does
    not tell Justin it is Boundary Road Vet.
    """
    from . import vet_names

    return vet_names.display(merchant, limit=24)


def _group_by_month(rows: list[dict]) -> list[tuple]:
    """[(month label, month charge total, rows)] preserving newest-first order."""
    groups = []
    for row in rows:
        key = row["date"][:7]
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(row)
    return [
        (datetime.strptime(key, "%Y-%m").strftime("%b %Y").upper(), sum(abs(r["amount"]) for r in group), group)
        for key, group in groups
    ]


def totals(rows: list[dict]) -> dict:
    """Header figures. `reimbursed` is money Petcover actually paid (settled
    claims only — a hard fact from their letter). `outstanding` is our own
    excess/cap estimate for everything not yet settled, and is deliberately
    NOT reduced by Petcover's age contribution: that rate isn't recorded for
    any pet, and guessing it is exactly what the dashboard spec forbids. So
    the estimate reads high, and the card says so."""
    reimbursed = sum(r["paid"] for r in rows if r["paid"] is not None)
    outstanding = 0.0
    estimated = False
    for row in rows:
        if row["paid"] is not None:
            continue
        expected = row.get("expected") or {}
        if expected.get("available") and expected.get("value"):
            outstanding += expected["value"]
            estimated = True
    return {"reimbursed": reimbursed, "outstanding": outstanding, "outstanding_is_estimate": estimated}


def render(
    rows: list[dict],
    page: int = 1,
    total_rows: int | None = None,
    months: int = 12,
    agg: dict | None = None,
) -> bytes:
    """PNG bytes for one page of claim history. `rows` is already the page's
    slice; `total_rows` is the unpaged count for the subtitle. `agg` should be
    totals() over the FULL set, not this page — a header that summarised only
    the visible rows would contradict its own "12 of 21 claims" subtitle."""
    groups = _group_by_month(rows)
    agg = totals(rows) if agg is None else agg
    total_rows = len(rows) if total_rows is None else total_rows

    height = MARGIN + PAD + HEADER_H
    for _, _, group in groups:
        height += MONTH_H + ROW_H * len(group) + GROUP_GAP
    height += FOOTER_H + PAD + MARGIN

    img = Image.new("RGB", (W * S, height * S), BG)
    d = ImageDraw.Draw(img)

    def rrect(box, radius, fill):
        _rrect(d, box, radius, fill)

    def text(xy, s, font, fill, anchor="la", spacing=0):
        _text(d, xy, s, font, fill, anchor, spacing)

    f_title = _font("sans_bold", 19)
    f_sub = _font("sans", 11)
    f_month = _font("sans_bold", 11)
    f_date = _font("mono", 12)
    f_vet = _font("sans", 13)
    f_amt = _font("mono_bold", 14)
    f_stat = _font("sans_bold", 9)
    f_subl = _font("sans", 11)
    f_foot = _font("sans", 10)

    rrect((MARGIN, MARGIN, W - MARGIN, height - MARGIN), 18, CARD)
    x0, x1 = MARGIN + PAD, W - MARGIN - PAD
    y = MARGIN + PAD

    # Header: title left, two right-aligned stat columns (reimbursed | to come).
    text((x0, y), "Claim history", f_title, TXT)
    col_out, col_paid = x1, x1 - 118
    text((col_paid, y + 5), _money(agg["reimbursed"]), f_amt, OK, anchor="ra")
    out_prefix = "~" if agg["outstanding_is_estimate"] else ""
    text((col_out, y + 5), f"{out_prefix}{_money(agg['outstanding'])}", f_amt, TXT, anchor="ra")
    y += 30
    subtitle = f"{total_rows} claims · last {months} months"
    if page > 1 or total_rows > len(rows):
        shown = min(ROWS_PER_PAGE, len(rows))
        subtitle = f"{shown} of {total_rows} claims · last {months} months"
    text((x0, y), subtitle, f_sub, DIM)
    text((col_paid, y), "reimbursed", f_sub, FAINT, anchor="ra")
    text((col_out, y), "est. to come", f_sub, FAINT, anchor="ra")
    y += 26

    d.line([x0 * S, y * S, x1 * S, y * S], fill=LINE, width=S)
    y += 18

    for month, month_total, group in groups:
        text((x0, y), month, f_month, FAINT, spacing=1.1)
        text((x1, y + 1), _money(month_total), f_month, FAINT, anchor="ra")
        y += MONTH_H
        for row in group:
            # The row was worded where the flag/pet/condition were in hand
            # (claim_status.history_rows); status alone is the fallback.
            label = row.get("label") or _status_label(row["status"])
            fg, chip_bg = _STATUS_COLOURS.get(row["status"], (DIM, LINE))
            d.ellipse([x0 * S, (y + 6) * S, (x0 + 7) * S, (y + 13) * S], fill=fg)
            text((x0 + 17, y + 1), datetime.strptime(row["date"], "%Y-%m-%d").strftime("%d %b"), f_date, DIM)
            text((x0 + 72, y), _vet_name(row["merchant"]), f_vet, TXT)
            text((x1, y - 1), _money(abs(row["amount"])), f_amt, TXT, anchor="ra")

            chip_w = d.textlength(label.upper(), font=f_stat) / S + 18
            rrect((x0 + 17, y + 20, x0 + 17 + chip_w, y + 36), 8, chip_bg)
            text((x0 + 17 + chip_w / 2, y + 23), label.upper(), f_stat, fg, anchor="ma")

            detail = " · ".join(filter(None, [row.get("pet_name"), row.get("condition_text")]))
            if detail:
                text((x0 + 17 + chip_w + 10, y + 23), _clip(detail, 34), f_subl, DIM)
            y += ROW_H
        y += GROUP_GAP

    footer_y = height - MARGIN - PAD - 10
    d.line([x0 * S, (footer_y - 14) * S, x1 * S, (footer_y - 14) * S], fill=LINE, width=S)
    text((x0, footer_y), "Amounts are bank charges. Estimates exclude age contribution.", f_foot, FAINT)

    out = io.BytesIO()
    img.resize((W, height), Image.LANCZOS).save(out, format="PNG")
    return out.getvalue()


# One accent per action kind, reusing the status palette so a "send draft" row
# reads the same colour as a Drafted chip on the history card.
_ACTION_COLOURS = {
    "split_proposal": ((94, 214, 195), (20, 48, 46)),
    "unmatch": ((224, 108, 96), (48, 23, 21)),
    "confirm_resolved": ((228, 168, 78), (46, 36, 20)),
    "mark_sent": ((122, 168, 255), (26, 38, 62)),
    "invoice_request_sent": ((177, 148, 255), (40, 34, 64)),
    "assign_pet": ((255, 176, 66), (58, 42, 20)),
    "set_condition": ((255, 176, 66), (58, 42, 20)),
    "dismiss_mismatch": ((150, 158, 175), (44, 48, 58)),
    "blocked_insurer": ((224, 108, 96), (48, 23, 21)),
}

SUMMARY_ROW_H = 34
SUMMARY_SECTION_H = 26


def _summarise(actions: list[dict]) -> list[tuple]:
    """[(kind, title, count, total charge)] in first-seen order, so the caller's
    urgency ordering carries through to the card."""
    order, by_kind = [], {}
    for action in actions:
        kind = action["kind"]
        if kind not in by_kind:
            order.append(kind)
            by_kind[kind] = {"title": action["title"], "count": 0, "total": 0.0}
        by_kind[kind]["count"] += 1
        by_kind[kind]["total"] += abs(action["amount"])
    return [(k, by_kind[k]["title"], by_kind[k]["count"], by_kind[k]["total"]) for k in order]


def render_actions_summary(actions: list[dict], shown: int | None = None) -> bytes:
    """PNG overview of everything waiting on Justin, grouped by action kind.

    Blocked items get their own section: they're real money stuck (Echo's whole
    backlog sits behind one undefined insurer process) but no button can clear
    them, so mixing them into the tappable list would misrepresent both."""
    actionable = [a for a in actions if a["actionable"]]
    blocked = [a for a in actions if not a["actionable"]]
    live_groups = _summarise(actionable)
    blocked_groups = _summarise(blocked)

    height = MARGIN + PAD + HEADER_H
    height += SUMMARY_ROW_H * len(live_groups)
    if blocked_groups:
        height += SUMMARY_SECTION_H + SUMMARY_ROW_H * len(blocked_groups)
    height += FOOTER_H + PAD + MARGIN

    img = Image.new("RGB", (W * S, height * S), BG)
    d = ImageDraw.Draw(img)
    f_title = _font("sans_bold", 19)
    f_sub = _font("sans", 11)
    f_section = _font("sans_bold", 10)
    f_row = _font("sans", 13)
    f_amt = _font("mono_bold", 14)
    f_count = _font("mono_bold", 12)
    f_foot = _font("sans", 10)

    _rrect(d, (MARGIN, MARGIN, W - MARGIN, height - MARGIN), 18, CARD)
    x0, x1 = MARGIN + PAD, W - MARGIN - PAD
    y = MARGIN + PAD

    waiting = sum(abs(a["amount"]) for a in actions)
    _text(d, (x0, y), "Actions needed", f_title, TXT)
    _text(d, (x1, y + 5), _money(waiting), f_amt, TXT, anchor="ra")
    y += 30
    subtitle = f"{len(actionable)} you can action"
    if blocked:
        subtitle += f" · {len(blocked)} blocked"
    if shown is not None and shown < len(actionable):
        subtitle += f" · showing {shown}"
    _text(d, (x0, y), subtitle, f_sub, DIM)
    _text(d, (x1, y), "total charged", f_sub, FAINT, anchor="ra")
    y += 26
    d.line([x0 * S, y * S, x1 * S, y * S], fill=LINE, width=S)
    y += 18

    def rows(groups):
        nonlocal y
        for kind, title, count, total in groups:
            fg, chip_bg = _ACTION_COLOURS.get(kind, (DIM, LINE))
            _rrect(d, (x0, y + 3, x0 + 22, y + 21), 6, chip_bg)
            _text(d, (x0 + 11, y + 7), str(count), f_count, fg, anchor="ma")
            _text(d, (x0 + 34, y + 4), title, f_row, TXT)
            _text(d, (x1, y + 3), _money(total), f_amt, TXT, anchor="ra")
            y += SUMMARY_ROW_H

    rows(live_groups)
    if blocked_groups:
        y += 4
        _text(d, (x0, y), "BLOCKED — NEEDS A DECISION, NOT A TAP", f_section, FAINT, spacing=1.1)
        y += SUMMARY_SECTION_H - 4
        rows(blocked_groups)

    footer_y = height - MARGIN - PAD - 10
    d.line([x0 * S, (footer_y - 14) * S, x1 * S, (footer_y - 14) * S], fill=LINE, width=S)
    _text(d, (x0, footer_y), "Oldest first — a visit is unclaimable once a year old.", f_foot, FAINT)

    out = io.BytesIO()
    img.resize((W, height), Image.LANCZOS).save(out, format="PNG")
    return out.getvalue()
