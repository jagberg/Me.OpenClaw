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

CMD ["uvicorn", "openclaw.main:app", "--host", "0.0.0.0", "--port", "8000"]
