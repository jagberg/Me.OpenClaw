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
# (llama-3.3-70b, 100k tokens/day, no context cap). Blank LLM_MODEL = provider
# default. Cerebras (gpt-oss-120b) is selectable but its free tier is sold-out
# for this account (402 on every model, 2026-07). LLM_PROVIDER=gemini keeps the
# legacy backend (extract only) for rollback.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_RATE_LIMIT_PER_MIN = _int_env("LLM_RATE_LIMIT_PER_MIN", 5)
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
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

# Twice-daily Google Drive DB backup (drive_backup.py). Folder ID is from
# https://drive.google.com/drive/folders/1UAxtye0zKxRlZTIWya-GxMqQJK6RE0y2
DRIVE_BACKUP_FOLDER_ID = os.environ.get("DRIVE_BACKUP_FOLDER_ID", "1UAxtye0zKxRlZTIWya-GxMqQJK6RE0y2")
DRIVE_BACKUP_PREFIX = os.environ.get("DRIVE_BACKUP_PREFIX", "OpenClawBettyVet")
DRIVE_BACKUP_LOG_SUBFOLDER = os.environ.get("DRIVE_BACKUP_LOG_SUBFOLDER", "logs")
# Durable local record: written even if Drive itself is unreachable, so a
# backup failure is never silent (CLAUDE.md: failures must be visible).
DB_BACKUP_LOCAL_LOG = os.environ.get("DB_BACKUP_LOCAL_LOG", "./data/backup.log")
