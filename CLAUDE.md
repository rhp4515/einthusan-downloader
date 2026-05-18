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

**Run the Streamlit UI locally:**
```bash
.venv/bin/streamlit run app.py
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
# Accessible at http://<host>:8502
```

## Architecture

Two entry points share the same core library (`einthusan_dl.py`):
- **`app.py`** — Streamlit web UI (primary user-facing interface)
- **`einthusan_dl.py`** — also has a `main()` / argparse CLI for headless use

### `einthusan_dl.py` — two classes

**`EinthusanClient`** handles auth and scraping:
- Auth (tried in order): (1) cookie injection (`EINTHUSAN_COOKIES=sid=...`); (2) headless Chromium via Playwright (`_browser_login`) when username+password are set; (3) plain HTTP form POST fallback (`_form_login`) if Playwright is unavailable
- `_browser_login` mechanism: Einthusan's login is **not** a form POST. `UILogin` registers a `'Login'` handler on the `arc65.page` event bus (CSRF handled internally via `arc65.page.id`). `UILogin` is never a global — it lives in a closure. We call `arc65.page.send('Login', { Email, Password })` directly. The server always returns HTTP 200; success/failure is detected from `data.Event === 'UserMessage' && data.Data.Err`.
- `get_movie_info(url)` → scrapes `#UIVideoPlayer` element for `data-mp4-link`, `data-content-title`, `data-hls-link`; has 6 fallback extraction methods
- `_cdn_hosts_from_page(soup)` → decodes the base64 `data-ejpingables` attribute on `#UIVideoPlayer` to get the live CDN hostname list; falls back to `cdn1/cdn2/cdn3.einthusan.io`
- `_resolve_to_cdn(raw_url)` → raw IPs in `data-mp4-link` (e.g. `117.106.99.123`) time out; this method calls `_cdn_hosts_from_page` then probes each CDN via HEAD requests and returns the working hostname URL
- `download(url, dest_path, on_progress)` → streams download with tqdm / callback progress

**`RadarrClient`** wraps Radarr v3 API:
- `manual_import_analyze(folder, movie_id)` → GET `/api/v3/manualimport`; `filterExistingFiles` must be Python `False` (bool), not string
- `manual_import_approve(items, ...)` → POST `/api/v3/manualimport`; payload requires `id` from GET response, `movieId` as direct int, no `shouldReplace` (schema uses `additionalProperties: false`)
- `rescan_movie(movie_id)` → returns command ID (int)
- `wait_for_command(command_id, timeout)` → polls `GET /api/v3/command/{id}` until `completed`/`failed`

### `app.py` — two-phase Streamlit workflow

**Phase 1 (sync, main thread):** URL input → `EinthusanClient.login()` + `get_movie_info()` → Radarr TMDB lookup → user picks match → advances to `preview` step.

**Phase 2 (background thread):** `_background_import()` runs in a `daemon=True` thread. All output goes through `queue.Queue` stored in `st.session_state.msg_queue`. The main thread polls every 0.75 s via `st.rerun()`, draining the queue to update logs and progress bar. The `QueueLogHandler` bridges Python `logging` → queue, attached to the `einthusan_dl` logger (level must be set to `INFO` explicitly, not inherited from root).

**Phase 1 → Phase 2 session handoff:** `_run_preview` stashes the live `requests.Session` as `movie_info["_session"]`. Phase 2 reuses it via `EinthusanClient.__new__(EinthusanClient); client.session = movie_info["_session"]` — bypassing `__init__` so the already-authenticated session is reused without re-logging in.

**Import flow inside `_background_import`:**
1. Resolve Radarr tag + Tamil language ID
2. Add/find movie in Radarr → `rescan_movie` + `wait_for_command` to ensure folder exists before import (folder creation is async in Radarr)
3. Download file to `STAGING_DIR_HOST`
4. Set 664 permissions + `shutil.chown` from `DOWNLOAD_CHOWN`
5. `manual_import_analyze` → `manual_import_approve` — if this raises (e.g. 500), fallback to `downloaded_movies_scan` command
6. `rescan_movie` + `wait_for_command` to finalise

**UI state machine** (`st.session_state.step`): `input` → `preview` → `running` → `done` | `error`.

The download button uses `on_click` callback to set `download_clicked=True` *before* re-render (not inside the `if st.button()` block), ensuring the button is disabled immediately on click.

### Tests

- `tests/test_extraction.py` — uses `examples/einthusan_source.html` as fixture; instantiates `EinthusanClient` without `__init__` via `__new__` to avoid network calls
- `tests/test_radarr_import.py` — mocks `RadarrClient._get` / `_post` / `session.put`; `SAMPLE_IMPORT_ITEM` must include `"id"` field (required by Radarr's `ManualImportReprocessResource` schema)

### Key env vars

| Variable | Notes |
|---|---|
| `EINTHUSAN_COOKIES` | `sid=<value>` from browser DevTools — preferred over username/password |
| `STAGING_DIR_HOST` | Staging folder path (same mount point used by both the downloader and Radarr containers) |
| `RADARR_ROOT_FOLDER` | Movies root path inside Radarr's container |
| `RADARR_QUALITY_PROFILE_ID` | Radarr quality profile ID (default: `1`) |
| `RADARR_LANGUAGE_PROFILE_ID` | Radarr language profile ID (default: `1`) |
| `DOWNLOAD_CHOWN` | e.g. `arr-user:users` — must match arr-stack PUID:PGID |

`docker-compose.yaml` hardcodes `STAGING_DIR_HOST=/data/media/manual_imports` (the container-internal mount point); both the downloader and Radarr containers share the same mount path so no separate Radarr path is needed.
