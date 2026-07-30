"""Re-extract the cached invoice extractions so line items carry their own dates.

`email_extractions` caches successful extraction forever, and the standing rule is
to invalidate when what extraction must return changes — the prompts now ask for a
per-line-item date. Approved token spend: one extraction per cached email.

Re-extracts IN PLACE rather than deleting the rows, for two reasons:
  * deleting leaves `find_visit_by_date`'s cache fallback empty until each email
    happens to be re-read by a future match attempt, which for an already-matched
    claim is never;
  * some cached rows came from the VISION path (image-only scans). Re-running text
    extraction on those returns nothing, so a blind delete-and-refill would lose
    good data. A row is only replaced when the new result is non-empty.

**Three phases, and the separation is load-bearing.** The first cut held one write
transaction open across every LLM call, so `gemini.py`'s own `llm_calls` logging
insert could not get the write lock — and each call then failed as
`LLMUnavailableError: database is locked`, which reads like a provider outage and
is not one. Read, then call, then write.

Runs INSIDE the container. Dry-run by default; --apply commits. Backs up first.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")
from openclaw import gmail_client, invoice_matching  # noqa: E402

DB = "/data/openclaw.db"
# Dated per run: the 2026-07-28 attempt wrote this name and changed nothing
# (all 14 failed on a Groq 403), so a fixed name would overwrite a snapshot
# taken before a different state of the DB.
BACKUP = "/data/openclaw.db.bak-pre-item-dates-20260731"
APPLY = "--apply" in sys.argv


def out(s=""):
    sys.stdout.buffer.write((str(s) + "\n").encode("ascii", "replace"))


def item_dates(invoices):
    return sum(1 for inv in invoices if isinstance(inv, dict)
               for item in (inv.get("items") or []) if item.get("date"))


# --- phase 1: read, then get off the database -------------------------------
ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
ro.row_factory = sqlite3.Row
cached = [(r["message_id"], json.loads(r["extracted_json"] or "[]"))
          for r in ro.execute("SELECT message_id, extracted_json FROM email_extractions ORDER BY extracted_at")]
ro.close()
out(f"{len(cached)} cached extraction(s) - one LLM extraction each")
out()

# --- phase 2: fetch + extract, holding no DB transaction -------------------
svc = gmail_client.build_service()
updates, kept, failed = [], 0, 0
for message_id, before in cached:
    try:
        msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        fresh = invoice_matching._extract_invoices(gmail_client.full_message_text(svc, msg))
    except Exception as exc:  # noqa: BLE001
        out(f"{message_id}: FAILED ({type(exc).__name__}: {str(exc)[:90]}) - row left as it was")
        failed += 1
        continue
    if not fresh:
        out(f"{message_id}: text extraction returned nothing - KEPT old row ({len(before)} invoice(s))")
        kept += 1
        continue
    updates.append((message_id, fresh))
    out(f"{message_id}: {len(before)} -> {len(fresh)} invoice(s), "
        f"item dates {item_dates(before)} -> {item_dates(fresh)}")

# --- phase 3: one short write transaction ----------------------------------
out()
if not APPLY:
    out(f"DRY RUN - {len(updates)} row(s) would be replaced, {kept} kept, {failed} failed (LLM calls were still made)")
    sys.exit(0)

rw = sqlite3.connect(DB, timeout=30)
rw.execute("PRAGMA journal_mode=WAL")
dest = sqlite3.connect(BACKUP)
with dest:
    rw.backup(dest)
dest.close()
out(f"backup written: {BACKUP}")
now = datetime.now(timezone.utc).isoformat()
with rw:
    for message_id, fresh in updates:
        rw.execute("UPDATE email_extractions SET extracted_json = ?, extracted_at = ? WHERE message_id = ?",
                   (json.dumps(fresh), now, message_id))
rw.close()
out(f"COMMITTED - replaced {len(updates)}, kept {kept}, failed {failed}")
