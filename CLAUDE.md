# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Setup (use uv, not pip):**
```bash
uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python3
.venv/bin/playwright install chromium
```

**Run tests:**
```bash
.venv/bin/pytest tests/ -v
# Single test file:
.venv/bin/pytest tests/test_extraction.py -v
# Single test:
.venv/bin/pytest tests/test_radarr_import.py::TestManualImportApprove::test_language_is_tamil -v
```

**Run the API + Streamlit UI locally (two processes):**
```bash
.venv/bin/honcho start   # runs both `api` and `ui` from Procfile
# or individually:
.venv/bin/python -m api               # API on :8500
.venv/bin/streamlit run app.py        # UI on :8501, set EINTHUSAN_API_BASE to point at it
```

**Run the CLI directly:**
```bash
.venv/bin/python einthusan_dl.py <einthusan_url> [--debug] [--download-only] [--skip-download] [--radarr-only /path/to/file.mp4]
# List Radarr quality profiles and root folders:
.venv/bin/python einthusan_dl.py --list-profiles
```

**Docker:**
```bash
docker compose up --build -d
# UI at http://<host>:8502, API at http://<host>:8503
```

## Architecture

### Three layers share `einthusan_dl.py`

- **`einthusan_dl.py`** — `EinthusanClient` (auth + scraping) and `RadarrClient` (Radarr v3 API wrapper), plus a `main()`/argparse CLI for headless use. Unchanged low-level behavior; see below for exact mechanisms.
- **`importer.py`** — framework-free orchestration: `resolve_movie` (login + scrape + TMDB lookup via Radarr), `add_to_radarr` (add-or-reuse + tag, unmonitored by default), `run_download_and_import` (flip monitored, download with one retry via a freshly re-resolved session, manual import with `DownloadedMoviesScan` fallback). Every failure raises an `ImporterError` subclass carrying a stable `.code` string consumed by the API.
- **`api/`** — FastAPI + uvicorn service (`python -m api`, port 8500 by default) that is the *only* thing holding Einthusan/Radarr credentials at runtime. Exposes a job-based workflow (`POST /api/v1/movies` → `awaiting_verification` → `PATCH` to correct the TMDB match → `POST /api/v1/jobs/{id}/download` → poll `GET /api/v1/jobs/{id}`) backed by an in-memory `JobStore` and two `ThreadPoolExecutor`s (`resolve_pool`, 2 workers; `download_pool`, 1 worker — serial downloads). Auth is a static `X-Api-Key` header (`EINTHUSAN_API_KEY`). See `docs/superpowers/specs/2026-09-07-einthusan-http-api-design.md` for the full endpoint/error-code reference.
- **`app.py`** — Streamlit UI, now a thin client of the API via `api_client.py::EinthusanApiClient`. Holds no Einthusan/Radarr credentials — only `EINTHUSAN_API_BASE` + `EINTHUSAN_API_KEY`. State machine: `input → resolving → preview → running → done | error`, driven by polling `GET /api/v1/jobs/{id}` every ~1.5s instead of the old daemon-thread + `queue.Queue` bridge.

### `einthusan_dl.py` — `EinthusanClient` and `RadarrClient` details

**`EinthusanClient`** handles auth and scraping:
- Auth (tried in order): (1) cookie injection (`EINTHUSAN_COOKIES=sid=...`); (2) headless Chromium via Playwright (`_browser_login`) when username+password are set; (3) plain HTTP form POST fallback (`_form_login`) if Playwright is unavailable
- `_browser_login` mechanism: Einthusan's login is **not** a form POST. `UILogin` registers a `'Login'` handler on the `arc65.page` event bus (CSRF handled internally via `arc65.page.id`). `UILogin` is never a global — it lives in a closure. We call `arc65.page.send('Login', { Email, Password })` directly. The server always returns HTTP 200; success/failure is detected from `data.Event === 'UserMessage' && data.Data.Err`.
- `get_movie_info(url)` → scrapes `#UIVideoPlayer` element for `data-mp4-link`, `data-content-title`, `data-hls-link`; has 6 fallback extraction methods
- `_cdn_hosts_from_page(soup)` → decodes the base64 `data-ejpingables` attribute on `#UIVideoPlayer` to get the live CDN hostname list; falls back to `cdn1/cdn2/cdn3.einthusan.io`
- `_resolve_to_cdn(raw_url)` → raw IPs in `data-mp4-link` (e.g. `117.106.99.123`) time out; this method calls `_cdn_hosts_from_page` then probes each CDN via HEAD requests and returns the working hostname URL
- `download(url, dest_path, on_progress)` → streams download with tqdm / callback progress

**`RadarrClient`** wraps Radarr v3 API:
- `manual_import_analyze(folder, movie_id)` → GET `/api/v3/manualimport`; `filterExistingFiles` must be Python `False` (bool), not string
- `manual_import_approve(items, ...)` → POST `/api/v3/command` with `{"name": "ManualImport", "importMode": "move", "files": [...]}`; returns the command ID. **Do not POST to `/api/v3/manualimport`** — that is only the *reprocess* endpoint: it re-runs the analysis, echoes the decisions back with a 200 and imports nothing (silent no-op). Each file entry requires `id` from the GET response, `movieId` as direct int, no `shouldReplace` (schema uses `additionalProperties: false`)
- `movie_has_file(movie_id)` → GET `/api/v3/movie/{id}` `hasFile`; a ManualImport command reports success even when every file was rejected, so this is the only reliable confirmation the import landed
- `rescan_movie(movie_id)` → returns command ID (int)
- `wait_for_command(command_id, timeout)` → polls `GET /api/v3/command/{id}` until `completed`/`failed`

### Tests

- `tests/test_extraction.py` — uses `examples/einthusan_source.html` as fixture; instantiates `EinthusanClient` without `__init__` via `__new__` to avoid network calls
- `tests/test_radarr_import.py` — mocks `RadarrClient._get` / `_post` / `session.put`; `SAMPLE_IMPORT_ITEM` must include `"id"` field (required by Radarr's `ManualImportReprocessResource` schema)

### Key env vars

| Variable | Notes |
|---|---|
| `EINTHUSAN_COOKIES` | `sid=<value>` from browser DevTools — preferred over username/password |
| `EINTHUSAN_API_KEY` | Static API key for authentication (the `X-Api-Key` header) |
| `API_PORT` | Port for the HTTP API service (default: `8500`) |
| `EINTHUSAN_API_BASE` | URL of the API service (used by `app.py`; defaults to `http://localhost:8500` locally, `http://einthusan-api:8500` in Docker) |
| `STAGING_DIR_HOST` | Staging folder path as seen by this API process |
| `STAGING_DIR_RADARR` | Staging folder path as seen by Radarr itself (optional, defaults to `STAGING_DIR_HOST`; set separately when Radarr runs in its own container/host with a different mount point for the same shared folder — used only for the `manual_import_analyze`/`downloaded_movies_scan` calls) |
| `RADARR_ROOT_FOLDER` | Movies root path inside Radarr's container |
| `RADARR_QUALITY_PROFILE_ID` | Radarr quality profile ID (default: `1`) |
| `RADARR_LANGUAGE_PROFILE_ID` | Radarr language profile ID (default: `1`) |

The API service (`api/` package) is the only component that reads Einthusan/Radarr credentials at runtime. The Streamlit UI holds only `EINTHUSAN_API_BASE` and `EINTHUSAN_API_KEY`, making it stateless and credential-safe.
