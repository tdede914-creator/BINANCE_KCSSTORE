FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps needed by pandas / cryptography / numpy wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Optional pip mirror for faster installs (esp. from China). Override via
# --build-arg PIP_INDEX_URL=... if you want to use a different mirror.
ARG PIP_INDEX_URL=https://pypi.org/simple

# Copy dependency list first for better layer caching
COPY requirements.txt ./
RUN pip install --index-url "$PIP_INDEX_URL" -r requirements.txt

# Copy source
COPY . .

# Data directory (for SQLite dev DB + logs)
RUN mkdir -p /app/data

EXPOSE 8000

# In development we honour the bind mount from docker-compose.yml
# (./backend -> /app) by enabling --reload so that `git pull` on the host
# is picked up without a manual container restart. In production the
# reload watchers are disabled and the baked-in image code is used.
#
# We wrap in a shell so we can branch on APP_ENV at container start
# instead of building two separate images.
CMD ["sh", "-c", "if [ \"$APP_ENV\" = \"development\" ]; then exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app/app; else exec uvicorn app.main:app --host 0.0.0.0 --port 8000; fi"]
