FROM python:3.13-slim

WORKDIR /app

# DejaVu fonts: the /history claim card renders text with Pillow, and the slim
# image ships no fonts at all (claim_card falls back to an unstyled default
# without these).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Deploy identity, stamped on every logged Telegram message so behaviour can be
# traced to the commit that produced it. Set by scripts/deploy.ps1; declared
# after the pip layer so bumping it doesn't invalidate the dependency cache.
ARG APP_VERSION=unknown
ENV APP_VERSION=$APP_VERSION

COPY openclaw ./openclaw

# Single worker, and that is load-bearing — do NOT add `--workers` without
# reading this. Overlap protection for the scheduled jobs (tick, ingest, nudge)
# is a per-job in-process lock in internal_api. Each worker would get its OWN
# lock and see nothing of the others, so the protection disappears silently —
# no error, no log line, just two ticks running at once. Two concurrent
# pipeline.run_once calls batch the same claims into two Gmail drafts, i.e. two
# Petcover submissions for one set of invoices, plus duplicate rows in the
# append-only claim_status_events. If this app ever needs more than one worker,
# the lock has to move into the database first (design notes: the openclaw-
# gateway-core change, decision D6 — a lock row cannot be naive, because a row
# saying "running" outlives the process that wrote it).
CMD ["uvicorn", "openclaw.main:app", "--host", "0.0.0.0", "--port", "8000"]
