# ── Build stage: install dependencies ───────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# System deps required to download/run Chromium via Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy only what's needed to resolve deps
COPY requirements.txt .

# Create a venv and install deps into it (no dev deps like pytest)
RUN uv venv /app/.venv && \
    uv pip install \
        --python /app/.venv/bin/python3 \
        --no-cache \
        requests beautifulsoup4 tqdm python-dotenv lxml "streamlit>=1.40.0" "playwright>=1.44.0" \
        "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0"

# Download Chromium browser binaries into a known location
ENV PLAYWRIGHT_BROWSERS_PATH=/pw-browsers
RUN /app/.venv/bin/playwright install chromium --with-deps

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Chromium runtime system libraries (installed by playwright --with-deps above,
# but we need them in the runtime image too)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Copy the venv and Chromium binaries from the builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /pw-browsers /pw-browsers

# Tell Playwright where to find the browsers at runtime
ENV PLAYWRIGHT_BROWSERS_PATH=/pw-browsers

# Streamlit config via env vars — works for any UID, no ~/.streamlit needed
ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ENABLE_CORS=false \
    STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Copy application code
COPY einthusan_dl.py app.py importer.py api_client.py ./
COPY api ./api

# Make the venv and browser binaries world-readable so the container can
# run as a non-root UID (e.g. arr-user 1006:100 set in docker-compose).
RUN chmod -R a+rX /app /pw-browsers

EXPOSE 8501 8000

# Health check — uses Python so no extra tools needed in the slim image
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD /app/.venv/bin/python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" \
        || exit 1

CMD ["/app/.venv/bin/streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
