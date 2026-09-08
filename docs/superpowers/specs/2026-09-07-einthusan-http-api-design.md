# Einthusan Downloader HTTP API — Design

**Date:** 2026-09-07
**Status:** Approved (brainstorming)
**Branch:** `feat/http-api`

## Motivation

The einthusan-downloader backend today has two entry points — a Streamlit
web UI (`app.py`) and an argparse CLI (`einthusan_dl.py`) — both calling the
core library (`EinthusanClient`, `RadarrClient`) directly. There is no
programmatic interface.

The user runs a separate Flutter app (`arrstack-app`) as a companion client
for a self-hosted *arr stack (Radarr, Sonarr, Prowlarr, qBittorrent,
Jellyseerr, …). They want that app to also drive Einthusan downloads:

1. Submit an Einthusan movie URL and get back the resolved TMDB match (as a
   themoviedb.org link) plus job state, so a human can confirm the match is
   correct.
2. After verifying the match, trigger the download + Radarr import with a
   second call, and watch progress.

This design adds a **FastAPI HTTP API** as the single code path for the
add → verify → download → import workflow. The Streamlit UI is refactored to
consume that API, so Streamlit and Flutter are both just clients. The CLI is
left as-is this round.

## Goals

- A small, versioned HTTP API (`/api/v1`) covering: create a job from an
  Einthusan URL, poll job state/progress/logs, correct a wrong TMDB match,
  begin the download, cancel/clean up, list recent jobs.
- API auth by static key (`X-Api-Key`), matching the `arrstack-app`
  `ApiKeyInterceptor` convention.
- Extract the orchestration currently inside `app.py::_background_import`
  into a framework-free `importer.py` module.
- Refactor `app.py` to call the API over HTTP (via a new `api_client.py`),
  deleting its background thread, queue, log-handler bridge, and the
  `EinthusanClient.__new__` session-handoff hack.
- Docker Compose runs the API and the UI as two services from one image.
- A local-dev runner (`Procfile` + `honcho`) starts both processes.
- Test coverage for `importer.py`, the API routes, and `api_client.py`.

## Non-Goals

- **Flutter client work.** The `arrstack-app` service plugin, repository,
  providers, and screens are a separate spec in that repo, written once this
  API contract is real.
- **CLI refactor.** `einthusan_dl.py::run()` keeps its direct library calls.
  Deduping it through `importer.py` is a noted future cleanup.
- **Persistent job store.** Jobs live in memory in the single API process
  (see Decision 4). No database, no cross-restart resume.
- **Multi-user / RBAC.** One shared API key. Single-operator homelab.
- **Parallel downloads.** Downloads run serially (Decision 5).
- **Search by title.** The API only accepts a full Einthusan movie URL, as
  the backend does today.
- **HTTPS / TLS termination.** Assumed handled upstream (reverse proxy) or
  trusted LAN, as with the existing Streamlit UI.

## Decisions (from brainstorming)

| # | Decision |
|---|----------|
| 1 | Scope: **backend API only**. Flutter client is a later, separate spec. |
| 2 | Framework: **FastAPI + uvicorn**, run as a **second process in the same Docker image** (not replacing Streamlit). |
| 3 | Progress delivery: **client polls `GET /api/v1/jobs/{id}`**. No SSE/WebSocket. |
| 4 | Job store: **in-memory `dict`** in the single uvicorn process, guarded by a `threading.Lock`. Lost on restart; partial staging files left for manual cleanup. |
| 5 | Concurrency: **serial downloads** via a 1-worker pool; `resolving` runs on a separate 2-worker pool. |
| 6 | Radarr add happens during **`resolving`** (the auto-picked match is added unmonitored + tagged). A wrong pick is corrected with `PATCH`, which removes the wrong movie and adds the right one. |
| 7 | `PATCH` `tmdb_id` **must be one of the job's returned `candidates`** (`422` otherwise). No arbitrary tmdbId — the user does not plan to input tmdbIds manually. |
| 8 | Streamlit **consumes the API** over HTTP like any other client. |
| 9 | API auth: **static `X-Api-Key`** vs `EINTHUSAN_API_KEY` env var, on every route except `/health`. |
| 10 | The authenticated Einthusan `requests.Session` lives **server-side in the job**; it is never serialized or handed to clients. |

## Architecture

```
        app.py (Streamlit)          arrstack-app (Flutter, later)
              │                              │
              └────── HTTP + X-Api-Key ──────┘
                             ▼
                   api/  (FastAPI)  ──────────  in-memory JobStore
                   routes · jobs · worker · auth
                             ▼
                   importer.py   ← orchestration: login, scrape, CDN resolve,
                             ▼      Radarr add/remove, download, manual import
                   einthusan_dl.py  ← EinthusanClient / RadarrClient (+ CLI, unchanged)
```

### Layer responsibilities

**`einthusan_dl.py`** — unchanged. `EinthusanClient` (auth, `get_movie_info`,
`_resolve_to_cdn`, `download`) and `RadarrClient` (`lookup_movie`,
`get_existing_movie`, `add_movie`, `update_movie_tags`, `get_or_create_tag`,
`get_language_id`, `manual_import_analyze`, `manual_import_approve`,
`downloaded_movies_scan`, `rescan_movie`, `wait_for_command`,
`list_quality_profiles`, `list_root_folders`). If `_resolve_to_cdn` /
`_cdn_hosts_from_page` need calling from `importer.py`, add thin public
aliases (`resolve_to_cdn`, `cdn_hosts_from_page`) that delegate; no logic
moves.

**`importer.py`** (new, framework-free) — orchestration extracted from
`app.py::_background_import`:

```python
@dataclass(frozen=True)
class TmdbCandidate:
    tmdb_id: int
    title: str
    year: int
    tmdb_url: str            # https://www.themoviedb.org/movie/{tmdb_id}
    poster_url: str | None

@dataclass
class ResolvedMovie:
    einthusan_title: str
    einthusan_year: int
    einthusan_url: str
    video_url: str           # CDN-resolved, ready to stream
    session: requests.Session  # authenticated; kept server-side only
    candidates: list[TmdbCandidate]  # from RadarrClient.lookup_movie, in Radarr order

def resolve_movie(cfg: dict, einthusan_url: str, *,
                  on_log: Callable[[str], None] | None = None) -> ResolvedMovie:
    # EinthusanClient(...).login() → get_movie_info(url) → _resolve_to_cdn
    # → RadarrClient.lookup_movie(title, year); raises ResolveError on failure

def add_to_radarr(cfg: dict, candidate: TmdbCandidate, *,
                  monitored: bool = False,
                  on_log=None) -> int:
    # get_or_create_tag("einthusan"); if get_existing_movie(tmdb_id):
    #   reuse + update_movie_tags; else add_movie(..., monitored=monitored,
    #   tags=[einthusan_tag]); returns radarr_movie_id

def remove_from_radarr(cfg: dict, radarr_movie_id: int, *,
                       delete_files: bool = False, on_log=None) -> None

def run_download_and_import(cfg: dict, *, resolved: ResolvedMovie,
                            candidate: TmdbCandidate, radarr_movie_id: int,
                            on_progress: Callable[[int, int], None] | None = None,
                            on_log=None) -> Path:
    # set movie monitored=True; build staging dest path (detect_extension,
    # safe_filename); rescan_movie + wait_for_command (folder exists);
    # EinthusanClient reusing resolved.session → download(video_url, dest, on_progress);
    # manual_import_analyze → filter to filename → manual_import_approve
    #   (Tamil language, release group "einthusan", movie_id);
    # on any exception → downloaded_movies_scan fallback → log "import manually";
    # rescan_movie + wait_for_command; returns dest path. Raises DownloadError
    # / ImportError subclasses of ImporterError.
```

Exceptions: `ImporterError` base; `ResolveError`, `DownloadError`,
`ImportError`, `RadarrUnavailableError`, `DownloadCancelled`. Each carries a
stable `code` string used by the API error envelope.

**`app.py::_background_import`** is deleted once `app.py` is cut over to
`api_client.py`. It is not rewritten to call `importer.py` — Streamlit never
touches the orchestration layer directly; only the API worker does.

### `api/` package

| File | Responsibility |
|---|---|
| `api/__init__.py` | exports `app` |
| `api/__main__.py` | `uvicorn.run("api:app", host="0.0.0.0", port=settings.api_port)` — `python -m api` |
| `api/main.py` | `FastAPI(title=…, version="1")`; include router at `/api/v1`; exception handlers mapping `ImporterError` + API errors → the error envelope; unauthenticated `/api/v1/health` |
| `api/routes.py` | the seven endpoints; thin — parse request model, call `JobStore` / `worker`, return response model |
| `api/models.py` | Pydantic v2 request/response models (shapes in "API surface" below) |
| `api/jobs.py` | `JobState` enum; `Job` dataclass (state, timestamps, `ResolvedMovie` ref, `match`, `candidates`, `progress`, bounded `deque` of log lines, `error`, `radarr_movie_id`, cancel flag); `JobStore` (dict + `Lock`, `create`, `get`, `list`, transition guards, 24h terminal-state reaper thread) |
| `api/worker.py` | two `ThreadPoolExecutor`s — `resolve_pool` (max_workers=2), `download_pool` (max_workers=1); `submit_resolve(job_id)` and `submit_download(job_id)` run `importer` calls and write results/progress/logs back into the `Job` under the store lock; honour the cancel flag between chunks |
| `api/auth.py` | `require_api_key` FastAPI dependency — compares `X-Api-Key` to `settings.api_key` with `secrets.compare_digest`; raises `401` envelope on mismatch/absence |
| `api/settings.py` | `Settings` loaded once from env: reuses `einthusan_dl.load_config()` for the Einthusan/Radarr block; adds `api_key` (`EINTHUSAN_API_KEY`, required — startup fails fast if unset), `api_port` (`API_PORT`, default 8000), `job_retention_hours` (default 24) |

`einthusan_dl` and its client classes are imported as-is.

**`api_client.py`** (new, top-level) — `EinthusanApiClient` wrapping the API
with `requests`:

```python
class EinthusanApiClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0)
    def health(self) -> dict
    def create_job(self, einthusan_url: str) -> JobView          # POST /movies -> 202
    def get_job(self, job_id: str) -> JobView                    # GET /jobs/{id}
    def patch_job(self, job_id: str, tmdb_id: int) -> JobView    # PATCH /jobs/{id}
    def start_download(self, job_id: str, tmdb_id: int | None = None) -> JobView
    def delete_job(self, job_id: str, *, remove_from_radarr: bool = False) -> None
    def list_jobs(self, *, state: str | None = None, limit: int = 50) -> list[JobView]
```

Raises `EinthusanApiError(code, message, status)` decoded from the error
envelope; `code="unreachable"` on a connection error / non-JSON body.
`JobView` is a frozen dataclass mirroring the job response.

### `app.py` refactor

Deleted: `_background_import`, `QueueLogHandler`, `ListLogHandler`, the
`queue.Queue` in session state, the daemon thread, the
`EinthusanClient.__new__(...)` session handoff.

Kept: the `st.session_state.step` machine (`input → preview → running →
done | error`) and the `st.rerun()` poll loop (now polling the API instead
of draining a queue).

New flow:

| Step | Action |
|---|---|
| `input` | user submits URL → `api_client.create_job(url)` → store `job_id`, go to `running` (state `resolving`) |
| `running` while `resolving` | poll `get_job`; show `log` lines; on `awaiting_verification` → `preview`; on `resolve_failed` → `error` |
| `preview` | show `job.match` (poster, title, year, `tmdb_url` as a link button) + `job.candidates`; buttons: **Confirm & download** → `start_download(job_id, match.tmdb_id)`; **Wrong match →** pick a candidate → `patch_job(job_id, tmdb_id)` (re-render) |
| `running` while `downloading`/`importing` | poll `get_job`; progress bar from `job.progress.percent`; show `log` |
| `done` | show `job.result` summary (title, year, TMDB link, Radarr movie id, file) |
| `error` | show `job.error.message` + `log`; offer restart |

Startup: read `EINTHUSAN_API_BASE` + `EINTHUSAN_API_KEY`; on any connection
failure to `health()` show a blocking banner ("Einthusan API not reachable
at `<base>` — is the `einthusan-api` service running?") and disable the
form. The sidebar keeps its per-session override fields but they now set
`EINTHUSAN_API_BASE` / `EINTHUSAN_API_KEY` only; the Einthusan and Radarr
credentials move entirely server-side (API process).

## API surface

Base path `/api/v1`. All routes except `/health` require
`X-Api-Key: <EINTHUSAN_API_KEY>` → `401` envelope otherwise. Request and
response bodies are JSON.

### `GET /api/v1/health` — unauthenticated

`200 { "status": "ok" }`. Used by the Docker healthcheck.

### `POST /api/v1/movies` — create a job

```jsonc
// request
{ "einthusan_url": "https://einthusan.tv/premium/movie/watch/XXXX/" }
```
- `422` envelope (`invalid_url`) if the URL is missing or not an
  `einthusan.tv` movie-watch URL (validated by regex).
- `202`:
```jsonc
{ "job_id": "b1f2c3d4-…", "state": "resolving" }
```
The resolve runs on `resolve_pool`: login → scrape → CDN resolve → TMDB
lookup → `add_to_radarr(top_candidate, monitored=False)`. On success the job
moves to `awaiting_verification`; on failure to `resolve_failed` with
`error`.

### `GET /api/v1/jobs/{job_id}` — poll

`404` envelope (`job_not_found`) if unknown or already reaped. `200`:

```jsonc
{
  "job_id": "b1f2c3d4-…",
  "state": "awaiting_verification",
  "created_at": "2026-09-07T23:10:00Z",
  "updated_at": "2026-09-07T23:10:18Z",
  "einthusan": {
    "title": "Vaaranam Aayiram",
    "year": 2008,
    "url": "https://einthusan.tv/premium/movie/watch/XXXX/"
  },
  "match": {
    "tmdb_id": 20880,
    "title": "Vaaranam Aayiram",
    "year": 2008,
    "tmdb_url": "https://www.themoviedb.org/movie/20880",
    "radarr_movie_id": 412,
    "poster_url": "https://image.tmdb.org/t/p/w500/…jpg"
  },
  "candidates": [
    { "tmdb_id": 20880, "title": "Vaaranam Aayiram", "year": 2008,
      "tmdb_url": "https://www.themoviedb.org/movie/20880",
      "poster_url": "https://image.tmdb.org/t/p/w500/…jpg" },
    { "tmdb_id": 553216, "title": "…", "year": 2011,
      "tmdb_url": "https://www.themoviedb.org/movie/553216", "poster_url": null }
  ],
  "progress": null,
  "log": [
    "Logging in to Einthusan.tv …",
    "Detected: Vaaranam Aayiram (2008)",
    "Found 2 TMDB result(s); auto-matched tmdbId=20880",
    "Added to Radarr (id=412), unmonitored, tag 'einthusan'"
  ],
  "error": null
}
```

State-specific fields:

- `resolving` — `match`, `candidates` null/empty; `log` grows.
- `resolve_failed` — `error: { code, message }`; terminal.
- `awaiting_verification` — `match` + `candidates` populated; `progress` null.
- `downloading` —
  ```jsonc
  "progress": { "downloaded_bytes": 734003200, "total_bytes": 1610612736,
                "percent": 45.6, "speed_bps": 5242880, "eta_seconds": 167 }
  ```
- `importing` — `progress.percent` 100; `log` shows import steps.
- `done` — terminal; adds
  ```jsonc
  "result": { "file": "/data/media/manual_imports/Vaaranam Aayiram (2008).mp4",
              "radarr_movie_id": 412, "tmdb_id": 20880 }
  ```
- `error` — `error: { code, message }`; terminal; partial file may remain.

`log` is the last N (default 200) lines.

### `PATCH /api/v1/jobs/{job_id}` — correct the match

```jsonc
{ "tmdb_id": 553216 }
```
- `404` `job_not_found`.
- `409` `invalid_state` if not `awaiting_verification`.
- `422` `tmdb_not_in_candidates` if `tmdb_id` is not in `candidates`.
- `200` updated job view. Backend: `remove_from_radarr(old radarr_movie_id)`
  then `add_to_radarr(chosen candidate, monitored=False)`; updates `match`.
  If the chosen candidate equals the current match, it is a no-op `200`.

### `POST /api/v1/jobs/{job_id}/download` — begin download

```jsonc
{ "tmdb_id": 553216 }   // optional guard
```
- `404` `job_not_found`.
- `409` `invalid_state` if not `awaiting_verification`.
- `409` `invalid_state` (message: match changed) if `tmdb_id` is given and
  differs from the job's current `match.tmdb_id`.
- `202`:
```jsonc
{ "job_id": "b1f2c3d4-…", "state": "downloading" }
```
Backend enqueues `submit_download(job_id)` on the 1-worker `download_pool`;
if a download is already running, this job waits (still reported as
`downloading` — see Open Questions for whether to add a `queued` sub-state).
`run_download_and_import` sets the movie monitored, downloads to staging,
and runs the manual import (with `downloaded_movies_scan` fallback).

### `DELETE /api/v1/jobs/{job_id}` — cancel / clean up

Query: `?remove_from_radarr=false` (default).
- `404` `job_not_found`.
- `204`: sets the cancel flag (a running download stops at the next chunk),
  deletes the partial staging file if present, drops the job from the store.
  If `remove_from_radarr=true`, also `remove_from_radarr(radarr_movie_id,
  delete_files=false)`.

### `GET /api/v1/jobs` — list

Query: `?state=<JobState>&limit=50` (max 200). `200`:
```jsonc
{ "jobs": [ { …job view… }, … ] }   // newest first, in-memory only
```

### Error envelope

Every 4xx/5xx response body:
```jsonc
{ "error": { "code": "invalid_state",
             "message": "Job b1f2… is downloading, not awaiting_verification" } }
```

| code | status | when |
|---|---|---|
| `unauthorized` | 401 | missing / wrong `X-Api-Key` |
| `invalid_url` | 422 | not a valid Einthusan movie URL |
| `job_not_found` | 404 | unknown or reaped job id |
| `invalid_state` | 409 | operation not allowed in the job's current state |
| `tmdb_not_in_candidates` | 422 | `PATCH` tmdb_id not among candidates |
| `resolve_failed` | (in job body) | login / scrape / lookup failed |
| `download_failed` | (in job body) | streaming the file failed |
| `import_failed` | (in job body) | Radarr import + fallback both failed |
| `radarr_unavailable` | 502 | Radarr unreachable during a sync call (add/patch) |
| `internal` | 500 | uncaught — message is generic, detail logged server-side |

## Data flow — happy path

1. `POST /api/v1/movies {einthusan_url}` → `202 {job_id, "resolving"}`.
2. `resolve_pool`: `resolve_movie` (Playwright login, scrape, CDN resolve,
   `RadarrClient.lookup_movie`) → `add_to_radarr(candidates[0], monitored=False)`
   → job `awaiting_verification` with `match` + `candidates`.
3. Client polls `GET /api/v1/jobs/{id}` until `awaiting_verification`, shows
   `match.tmdb_url` to the user.
4. (Optional) match wrong → `PATCH {tmdb_id}` → Radarr movie swapped.
5. `POST /api/v1/jobs/{id}/download` → `202 {"downloading"}`.
6. `download_pool`: set monitored → `rescan` + `wait_for_command` → stream to
   staging (`on_progress` → `job.progress`) → `manual_import_analyze` →
   `manual_import_approve` (Tamil, group `einthusan`) → `rescan` +
   `wait_for_command`. Job → `importing` → `done` with `result`.
7. Client polls to `done`; shows the summary. Job reaped after 24h.

## Error handling

- **Resolve failures** (`ResolveError`) — bad credentials, Playwright
  missing, page structure changed, zero TMDB results — set the job
  `resolve_failed` with a human message; `log` retains the trail. No Radarr
  movie was added yet (add is the last resolve step), so nothing to clean.
  If the add itself fails → `radarr_unavailable` job error.
- **Download failures** (`DownloadError`) — CDN 403 / expired signed URL,
  connection reset, disk full. On a `403`/expired detection the worker
  retries once by re-running `resolve_movie` (fresh session + URL) before
  failing. Partial file left in staging; `DELETE` or a later job cleans it.
- **Import failures** — `manual_import_approve` raising (e.g. 500) triggers
  the existing `downloaded_movies_scan` fallback; if that also raises, job
  → `error` `import_failed` with a "import manually via Radarr UI" log line.
  The file is downloaded and on disk regardless.
- **Cancellation** — `DELETE` sets `job.cancelled`; `download()`'s
  `on_progress` callback checks it and raises `DownloadCancelled`, caught by
  the worker which cleans the partial file and drops the job.
- **API process restart** — all jobs lost. In-flight `downloading` jobs
  leave a partial file. Documented in `README.md`; acceptable for a
  single-instance homelab (Decision 4).
- **Streamlit ↔ API** — `api_client` maps connection errors and non-JSON
  responses to `EinthusanApiError(code="unreachable")`; `app.py` shows the
  blocking banner.
- **FastAPI** — a top-level exception handler converts any uncaught
  exception to `{"error": {"code": "internal", …}}` `500` and logs the
  stack via the `einthusan_dl` logger.

## Config & deployment

### Environment variables

| var | consumed by | notes |
|---|---|---|
| `EINTHUSAN_API_KEY` | API (required), Streamlit | shared secret; API fails fast at startup if unset |
| `EINTHUSAN_API_BASE` | Streamlit | e.g. `http://einthusan-api:8000`; default `http://localhost:8000` |
| `API_PORT` | API | default `8000` |
| `EINTHUSAN_COOKIES` / `EINTHUSAN_USERNAME` / `EINTHUSAN_PASSWORD` | API only | unchanged |
| `RADARR_URL` / `RADARR_API_KEY` / `RADARR_ROOT_FOLDER` / `RADARR_QUALITY_PROFILE_ID` / `RADARR_LANGUAGE_PROFILE_ID` | API only | unchanged |
| `STAGING_DIR_HOST` | API only | unchanged |
| `DOWNLOAD_CHOWN` | — | already dead; removed from compose in this work |

### Docker

One image (current `Dockerfile`, plus `fastapi`, `uvicorn[standard]` added
to `requirements.txt`; `requests` already present). `docker-compose.yaml`
gains a second service:

| service | command | ports | mounts / env |
|---|---|---|---|
| `einthusan-api` | `python -m api` | `8000:8000` | staging volume; all Einthusan/Radarr env; `EINTHUSAN_API_KEY` |
| `einthusan-ui` | `streamlit run app.py --server.port=8501 --server.address=0.0.0.0` | `8502:8501` | `EINTHUSAN_API_BASE=http://einthusan-api:8000`, `EINTHUSAN_API_KEY` |

`einthusan-ui` `depends_on: [einthusan-api]`. The `Dockerfile` `ENTRYPOINT`
becomes a small `entrypoint.sh` that dispatches on `$1` (`api` | `ui`), or
`ENTRYPOINT` is dropped and each service sets a full `command`. Healthchecks:
API hits `/api/v1/health`; UI keeps the Streamlit `_stcore/health` check.

### Local dev

`Procfile`:
```
api: .venv/bin/python -m api
ui: .venv/bin/streamlit run app.py
```
`honcho` added to `requirements.txt`. `honcho start` runs both.
`CLAUDE.md` and `README.md` updated with the API commands, env vars, and the
two-process model.

## Testing

- **`tests/test_importer.py`** (new) — mock `EinthusanClient` /
  `RadarrClient` (via `__new__` + attribute injection, as
  `test_extraction.py` does). Cover: `resolve_movie` candidate shaping and
  `tmdb_url` construction; `resolve_movie` raising `ResolveError` on login
  failure and on zero lookup results; `add_to_radarr` new vs existing movie;
  `remove_from_radarr`; `run_download_and_import` happy path; the
  `downloaded_movies_scan` fallback when `manual_import_approve` raises;
  `DownloadCancelled` handling.
- **`tests/test_api.py`** (new) — FastAPI `TestClient`; `importer` functions
  monkeypatched. Cover: `/health` no auth; every other route `401` without
  the key; `POST /movies` `202` + `invalid_url` `422`; job progression
  `resolving → awaiting_verification` (resolve mocked sync); `GET` `404`;
  `PATCH` success, `409` wrong state, `422` non-candidate; `download` `202`,
  `409` wrong state, `409` match-changed guard; `DELETE` `204` + store
  eviction; `GET /jobs` filter + limit; error-envelope shape on each.
- **`tests/test_api_client.py`** (new) — `EinthusanApiClient` against a
  mocked `requests` transport (`responses` library or a fake session):
  each method's request shape and response decode; `EinthusanApiError`
  from an error envelope; `code="unreachable"` on connection error.
- **`tests/test_extraction.py`**, **`tests/test_radarr_import.py`** —
  unchanged, must stay green.
- **`app.py`** — no automated tests (Streamlit runtime). Kept thin; manual
  smoke check: `honcho start`, add a real URL, verify, download, confirm the
  file imports into Radarr.
- Coverage target 80% for `importer.py`, `api/`, `api_client.py`.

## Rollout

1. Add deps (`fastapi`, `uvicorn[standard]`, `honcho`), `importer.py`, and
   `tests/test_importer.py`. `app.py` untouched and still working (its
   `_background_import` keeps its current direct library calls for now).
2. Add `api/` + `api_client.py` + `tests/test_api.py` +
   `tests/test_api_client.py`. API runnable standalone; Streamlit still on
   the old path.
3. Cut `app.py` over to `api_client.py`; delete `_background_import`,
   `QueueLogHandler`, `ListLogHandler`, the queue, the thread, the
   `__new__` handoff.
4. `Dockerfile` / `docker-compose.yaml` / `Procfile` / `CLAUDE.md` /
   `README.md`.
5. Manual end-to-end smoke test.

Each step keeps `pytest tests/ -v` green.

## Open questions for the implementation plan

- Exact `poster_url` source — `RadarrClient.lookup_movie` results include an
  `images` array; confirm the field/shape (`remoteUrl` vs `url`) during
  implementation.
- Whether `_resolve_to_cdn` needs the page `soup` re-fetched on the
  download-time retry, or the stored `ResolvedMovie` carries enough. Confirm
  against `einthusan_dl.py` when wiring `run_download_and_import`.
- `safe_filename` / `detect_extension` are referenced as module-level in
  `einthusan_dl.py` — confirm they are importable as-is (names and
  signatures).
- Whether to expose a `queued` state distinct from `downloading` when a job
  is waiting behind the 1-worker `download_pool`. Default: no — keep it
  `downloading` with an empty `progress` until bytes move.
