import os

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_RATE_LIMIT_PER_MIN = _int_env("GEMINI_RATE_LIMIT_PER_MIN", 15)

# LLM backend — provider-agnostic (ADR supersedes 0001). Any OpenAI-compatible
# provider works by pointing base_url + model + key at it; llm.py holds the
# per-provider base_url/default-model table. Default is Groq's free tier
# (llama-3.3-70b-versatile). Limits measured 2026-07-25, because this note and
# agent.py's "8k context cap" contradicted each other:
#   context window          131,072 tokens   (so the "8k cap" was simply wrong)
#   max completion tokens    32,768
#   tokens per MINUTE        12,000          (x-ratelimit-limit-tokens)
#   requests per day          1,000          (x-ratelimit-limit-requests)
#   tokens per DAY          100,000          <- the binding constraint
# The TPD limit does NOT appear in the rate-limit response headers — only in the
# body of the 429 it eventually throws. Measuring from headers alone produced a
# confident "no daily token limit exists", which a live 429 disproved the same
# hour. The original 100k/day note here was right; don't "correct" it again.
# At ~2.6k tokens per chat request (the tool schema ships every time), that's
# well under 40 requests a day — which is why the agent's tool loop stays tight.
# Blank LLM_MODEL = provider default. LLM_PROVIDER=gemini keeps the legacy backend (extract only) for
# rollback; Gemini also serves the vision-OCR fallback regardless of provider.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_RATE_LIMIT_PER_MIN = _int_env("LLM_RATE_LIMIT_PER_MIN", 5)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/openclaw.db")

GMAIL_CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", "./data/credentials.json")
GMAIL_TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "./data/token.json")
GMAIL_POLL_INTERVAL_MINUTES = _int_env("GMAIL_POLL_INTERVAL_MINUTES", 5)

PETCOVER_TEMPLATE_PATH = os.environ.get("PETCOVER_TEMPLATE_PATH", "./data/petcover-claim-template.pdf")
# Status polling ignores Petcover emails older than this (Gmail after: format).
# Guards against first-run backfill: without it the first poll would ingest
# years of historical replies about long-settled claims and could mis-correlate
# them onto currently open ones. Default = the date this feature shipped.
PETCOVER_STATUS_SINCE = os.environ.get("PETCOVER_STATUS_SINCE", "2026/07/18")
CLAIM_OUTPUT_DIR = os.environ.get("CLAIM_OUTPUT_DIR", "./data/claims")
# Per-visit invoice PDFs auto-extracted from vet reply emails (claim_forms.
# ensure_invoice_file). Inside app/data on purpose: it's the only host dir the
# container can see (compose binds app/data -> /data; Google Drive is not visible).
INVOICE_OUTPUT_DIR = os.environ.get("INVOICE_OUTPUT_DIR", "./data/invoices")
VET_CLAIM_PIPELINE_INTERVAL_MINUTES = _int_env("VET_CLAIM_PIPELINE_INTERVAL_MINUTES", 15)
INVOICE_MATCH_WINDOW_DAYS = _int_env("INVOICE_MATCH_WINDOW_DAYS", 3)

# Policyholder details for the claim form's "Your details" section — not
# tracked anywhere else in OpenClaw. Left blank (non-blocking) until set.
OWNER_NAME = os.environ.get("OWNER_NAME", "")
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")
OWNER_ADDRESS = os.environ.get("OWNER_ADDRESS", "")
OWNER_POSTCODE = os.environ.get("OWNER_POSTCODE", "")
OWNER_STATE = os.environ.get("OWNER_STATE", "")

# Invoices sometimes arrive forwarded from a spouse's address instead of the
# vet directly — searched as a fallback alongside the merchant-name query.
SPOUSE_EMAIL = os.environ.get("SPOUSE_EMAIL", "")

# Bank details for the claim form's payment section — same account for every
# claim regardless of pet, so kept owner-level rather than per-pet.
OWNER_BANK_ACCOUNT_NAME = os.environ.get("OWNER_BANK_ACCOUNT_NAME", "")
OWNER_BANK_BSB = os.environ.get("OWNER_BANK_BSB", "")
OWNER_BANK_ACCOUNT_NUMBER = os.environ.get("OWNER_BANK_ACCOUNT_NUMBER", "")

# Telegram bot: single authorized user, identified by username (not a manually
# copied chat ID — the bot self-registers its chat ID via /start, see telegram_bot.py).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USERNAME = os.environ.get("TELEGRAM_USERNAME", "jagberg")
# Which runtime polls Telegram. Default on = this app, which is the pre-cutover
# state. Turning it off hands the channel to the gateway, and that IS the
# cutover -- two pollers on one token is a 409 Conflict, so the two are exact
# opposites and `scripts/gateway_preflight.py` fails the deploy if both poll or
# neither does. Kept as a flag rather than a deletion for one week of real use
# after cutover (task 4.1): rollback is this env var and a restart, ~30s.
TELEGRAM_UPDATER_ENABLED = os.environ.get("TELEGRAM_UPDATER_ENABLED", "1").strip().lower()     not in ("0", "false", "no", "off")

# The OpenClaw gateway's side of the house. The gateway owns the bot token and
# the agent loop; this app owns the claims domain and calls out to it. Two
# deliberate absences here: no Gmail credential and no Google key of any kind
# belongs to the gateway (gmail-isolation-boundary), and the gateway is never
# given DATABASE_PATH — a read-write open from the wrong side deleted the WAL
# sidecars once and took the whole app down.
#
# Shared secret for the /internal routes the gateway calls (cron + the event
# bridge). Blank means the surface refuses every request rather than running
# open: an unset secret is a misconfiguration, not a permission.
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET", "")
# Host allowlist for those routes — defence in depth, the secret is the actual
# auth. Default is loopback only. A gateway running in its own compose service
# is NOT loopback, so that deployment has to widen this deliberately.
INTERNAL_API_ALLOW_HOSTS = {
    h.strip() for h in os.environ.get("INTERNAL_API_ALLOW_HOSTS", "127.0.0.1,::1").split(",") if h.strip()
}
# How outbound Telegram messages leave once the gateway owns the token. Every
# send goes through gateway_client so there is one logged seam — the same reason
# every send goes through LoggedBot today.
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "openclaw")
OPENCLAW_CLI_TIMEOUT_SECONDS = _int_env("OPENCLAW_CLI_TIMEOUT_SECONDS", 30)
# The CLI is multi-channel and defaults to nothing, so every invocation must
# name its channel. Configurable only because the flag takes a value; adding a
# second channel is a design decision (the authorization check is Telegram
# username-based and has no equivalent elsewhere), not an env change.
OPENCLAW_CHANNEL = os.environ.get("OPENCLAW_CHANNEL", "telegram")
# The gateway's own version, stamped by scripts/deploy.ps1 and surfaced on
# /health. It must never be written into `telegram_messages.app_version`: that
# column exists so the message log is a dataset keyed to the code that produced
# each row, and two runtimes mean two versions. Conflating them makes the
# dataset lie about which deploy handled a message (ADR-0014).
GATEWAY_VERSION = os.environ.get("GATEWAY_VERSION", "")
# The media outbox: one shared volume, two path spaces for the same file. The
# app writes to the first, and must hand the gateway the second — the gateway's
# media allowlist is a fixed set of roots, and a path outside them is refused
# with an error that reads like a permissions problem (media_outbox.py).
#
# These are the ONLY bytes that cross the container boundary. Widening either to
# reach `app/data` would undo the isolation the whole design rests on.
MEDIA_OUTBOX_DIR = os.environ.get("MEDIA_OUTBOX_DIR", "/data/outbox")
MEDIA_OUTBOX_GATEWAY_DIR = os.environ.get(
    "MEDIA_OUTBOX_GATEWAY_DIR", "/home/node/.openclaw/media"
)

# Twice-daily Google Drive DB backup (drive_backup.py). Folder ID is from
# https://drive.google.com/drive/folders/1UAxtye0zKxRlZTIWya-GxMqQJK6RE0y2
DRIVE_BACKUP_FOLDER_ID = os.environ.get("DRIVE_BACKUP_FOLDER_ID", "1UAxtye0zKxRlZTIWya-GxMqQJK6RE0y2")
DRIVE_BACKUP_PREFIX = os.environ.get("DRIVE_BACKUP_PREFIX", "OpenClawBettyVet")
DRIVE_BACKUP_LOG_SUBFOLDER = os.environ.get("DRIVE_BACKUP_LOG_SUBFOLDER", "logs")
# Durable local record: written even if Drive itself is unreachable, so a
# backup failure is never silent (CLAUDE.md: failures must be visible).
DB_BACKUP_LOCAL_LOG = os.environ.get("DB_BACKUP_LOCAL_LOG", "./data/backup.log")

# Deploy identity, baked into the image at build time (Dockerfile ARG, set by
# scripts/deploy.ps1 from the git short SHA + branch). Stamped on every logged
# message so behaviour can be attributed to the deploy that produced it.
# "unknown" means someone built without the script — main.py warns about it.
APP_VERSION = os.environ.get("APP_VERSION", "unknown")

# An inbound update still unprocessed after this long stops being replay-eligible:
# Telegram itself only retains updates ~24h, so anything older is moot. The row
# survives (it's training data) — only its place in the queue expires.
MESSAGE_QUEUE_TTL_HOURS = int(os.environ.get("MESSAGE_QUEUE_TTL_HOURS", "24"))

# Root log level. Default INFO because it was effectively WARNING (no
# basicConfig anywhere), which discarded every logger.info and left a morning's
# bot activity impossible to reconstruct.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# An outstanding action older than this gets one daily nudge. notify_claim_states
# dedupes on (status, flag) so it goes quiet on claims that never change — two
# drafted claims sat unsent for three days without a reminder.
ACTION_NUDGE_DAYS = int(os.environ.get("ACTION_NUDGE_DAYS", "3"))
ACTION_NUDGE_HOUR = int(os.environ.get("ACTION_NUDGE_HOUR", "9"))
# A vet practice is chased on a weekday, so unanswered information requests get
# their own weekly beat rather than competing in the daily stale-action summary.
# Same hour as that job — one definition of "morning", one thing to keep aligned.
VET_NUDGE_DAY = os.environ.get("VET_NUDGE_DAY", "mon")
# The claim's real clock: Petcover's own letter says a claim must be submitted
# within one year of the pet RECEIVING TREATMENT, so the deadline is anchored on
# the transaction date, not on when the request arrived.
INFO_REQUEST_DEADLINE_DAYS = int(os.environ.get("INFO_REQUEST_DEADLINE_DAYS", "365"))
