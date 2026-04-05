# ── Build stage: install dependencies ───────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy only what's needed to resolve deps
COPY requirements.txt .

# Create a venv and install deps into it (no dev deps like pytest)
RUN uv venv /app/.venv && \
    uv pip install \
        --python /app/.venv/bin/python3 \
        --no-cache \
        requests beautifulsoup4 tqdm python-dotenv lxml "streamlit>=1.40.0"

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy the venv from the builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY einthusan_dl.py app.py ./

# Streamlit config — disable the "hey, check out our cloud" popups
RUN mkdir -p /root/.streamlit && \
    printf '[general]\nemail = ""\n[browser]\ngatherUsageStats = false\n' \
    > /root/.streamlit/credentials.toml && \
    printf '[server]\nheadless = true\nport = 8501\nenableCORS = false\nenableXsrfProtection = false\n' \
    > /root/.streamlit/config.toml

EXPOSE 8501

# Health check — uses Python so no extra tools needed in the slim image
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD /app/.venv/bin/python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" \
        || exit 1

ENTRYPOINT ["/app/.venv/bin/streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
