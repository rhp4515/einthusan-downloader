# Einthusan Downloader

Download Tamil movies from [Einthusan.tv](https://einthusan.tv) (premium account required) and import them directly into Radarr so they appear in Jellyfin.

---

## How it works

1. Paste an Einthusan movie URL into the web UI
2. The API logs in, extracts the signed MP4 URL, and searches TMDB via Radarr
3. You confirm the correct TMDB match
4. The file downloads to your staging folder with a live progress bar
5. The API imports it into Radarr — tagged `einthusan`, language `Tamil`, release group `einthusan`

---

## Quick start (Docker)

```bash
# 1. Clone / copy this folder onto your server
cd /volume3/docker/personal/einthusan-downloader

# 2. Create your .env
cp .env.example .env
nano .env          # fill in credentials (see Configuration below)

# 3. Build and start
docker compose up -d --build

# 4. Open the UI
http://<your-server-ip>:8502

The HTTP API is available separately at http://<your-server-ip>:8503 (requires the `X-Api-Key` header — see Configuration).
```

---

## Configuration

All settings live in `.env`. The sidebar in the UI can override them per-session without touching the file.

```env
# Einthusan
EINTHUSAN_USERNAME=your@email.com
EINTHUSAN_PASSWORD=yourpassword
# Preferred over username/password — sid=<value> from browser DevTools
EINTHUSAN_COOKIES=

# Radarr
# Use host.docker.internal when running in Docker (points to the host machine).
# Use localhost:7878 when running the script directly on the host.
RADARR_URL=http://host.docker.internal:7878
RADARR_API_KEY=your_radarr_api_key
RADARR_ROOT_FOLDER=/data/media/movies      # path inside the Radarr container
RADARR_QUALITY_PROFILE_ID=1
RADARR_LANGUAGE_PROFILE_ID=1

# Paths
STAGING_DIR_HOST=/data/media/manual_imports    # as seen by the API container

# API service (api/)
EINTHUSAN_API_KEY=choose-a-long-random-value
API_PORT=8500

# UI service (app.py) — only needed when running app.py against a remote API
EINTHUSAN_API_BASE=http://localhost:8500
```

The Streamlit UI (`einthusan-ui` service) no longer needs Einthusan or Radarr
credentials directly — it only needs `EINTHUSAN_API_BASE` (defaults to
`http://einthusan-api:8500` inside Docker Compose) and `EINTHUSAN_API_KEY`.
All actual credentials live only in the `einthusan-api` service's `.env`.

### Finding your Radarr quality profile ID

```bash
.venv/bin/python3 einthusan_dl.py --list-profiles
```

---

## Docker details

```
host port 8502  →  einthusan-ui container port 8501 (Streamlit)
host port 8503  →  einthusan-api container port 8500 (HTTP API)
```

The container needs to reach Radarr. Because it doesn't share a Docker network with the arr-stack, it uses `host.docker.internal` — a hostname that resolves to the host machine from inside any container. The `extra_hosts` line in `docker-compose.yaml` enables this on Linux:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

If Radarr runs on a different machine, set `RADARR_URL=http://<radarr-ip>:7878` in `.env`.

The staging directory is mounted at the same path in both containers so Radarr can see the downloaded file:

```yaml
volumes:
  - /volume2/arr-data/media/manual_imports:/data/media/manual_imports
```

---

## Running without Docker

```bash
# One-time setup
bash setup.sh

# Verify Radarr connection and get profile IDs
.venv/bin/python3 einthusan_dl.py --list-profiles

# Web UI
.venv/bin/streamlit run app.py   # → http://localhost:8501

# CLI (headless)
.venv/bin/python3 einthusan_dl.py https://einthusan.tv/premium/movie/watch/XXXX/
```

### CLI flags

| Flag | Purpose |
|------|---------|
| `--debug` | Verbose logging — prints page HTML if URL extraction fails |
| `--download-only` | Download file, skip Radarr import |
| `--skip-download` | Skip download, only run Radarr import |
| `--radarr-only FILE` | Import an already-downloaded file into Radarr |
| `--list-profiles` | Print Radarr quality profiles and root folders |

---

## What Radarr receives

Every import sets:

| Field | Value |
|-------|-------|
| Language | Tamil |
| Release group | `einthusan` |
| Tag | `einthusan` (created automatically if absent) |

---

## Project layout

```
einthusan-downloader/
├── app.py              # Streamlit web UI
├── einthusan_dl.py     # Core logic (CLI + library)
├── Dockerfile
├── docker-compose.yaml
├── requirements.txt
├── setup.sh            # One-time local setup (creates .venv with uv)
├── .env.example
├── examples/
│   ├── einthusan_source.html   # Sample page HTML used in tests
│   ├── curl_command.sh         # Example CDN download curl
│   └── curl_raw_download.sh    # Example raw proxy download curl
└── tests/
    ├── test_extraction.py      # HTML scraping tests (25 cases)
    └── test_radarr_import.py   # Radarr API tests (18 cases)
```

---

## Running tests

```bash
.venv/bin/pytest tests/ -v
```

Tests use the real sample HTML in `examples/` and mock all network calls — no live Radarr or Einthusan connection needed.
