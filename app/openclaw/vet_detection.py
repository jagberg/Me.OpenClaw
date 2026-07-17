from datetime import datetime, timezone

from . import db, gemini

# Cheap heuristic first (keyword/allowlist), Gemini only for ambiguous cases —
# keeps the 15rpm free-tier budget off the hot path (design.md decision).
VET_KEYWORDS = [
    "vet",
    "veterinary",
    "animal hospital",
    "animal clinic",
    "animal emergency",
    "petbarn vet",
    "greencross",
    "vetwest",
    "sah",  # Sydney Animal Hospitals
    "sves",  # Sydney Veterinary Emergency & Specialists
    "medipaws",
]

# User-maintained allowlist for exact merchant names the keyword list misses.
VET_ALLOWLIST: set[str] = set()

AMBIGUOUS_CATEGORY_HINTS = ["medical", "pet", "animal"]

CLASSIFY_PROMPT = """Is this a bank transaction merchant for a veterinary clinic or pet-related \
medical service? Respond with ONLY "yes" or "no".

Merchant: {merchant}
Category: {category}
"""


def _keyword_match(merchant: str) -> bool:
    merchant_lower = merchant.lower()
    return any(k in merchant_lower for k in VET_KEYWORDS) or merchant in VET_ALLOWLIST


def _looks_ambiguous(category: str | None) -> bool:
    if not category:
        return False
    category_lower = category.lower()
    return any(h in category_lower for h in AMBIGUOUS_CATEGORY_HINTS)


def classify(merchant: str, category: str | None = None) -> bool:
    """Returns True if vet-related. Gemini is only called for the ambiguous case
    (medical/pet-adjacent category, no keyword hit)."""
    if _keyword_match(merchant):
        return True
    if _looks_ambiguous(category):
        raw = gemini.extract(
            CLASSIFY_PROMPT.format(merchant=merchant, category=category),
            purpose="vet_classification",
        )
        return raw.strip().lower().startswith("yes")
    return False


def _create_pending_claim(conn, transaction_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO vet_claims (transaction_id, status, created_at, updated_at) "
        "VALUES (?, 'pending_match', ?, ?)",
        (transaction_id, now, now),
    )


def classify_unflagged() -> int:
    """Classifies every bank_transactions row with vet_flag still NULL. Vet-flagged
    rows get a pending_match vet_claims row created. Returns count classified."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, merchant, category FROM bank_transactions WHERE vet_flag IS NULL"
        ).fetchall()

    for row in rows:
        flag = classify(row["merchant"], row["category"])
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE bank_transactions SET vet_flag = ? WHERE id = ?", (int(flag), row["id"])
            )
            if flag:
                _create_pending_claim(conn, row["id"])
    return len(rows)
