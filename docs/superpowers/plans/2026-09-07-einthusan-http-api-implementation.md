# Einthusan HTTP API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing Einthusan-download-and-Radarr-import pipeline as an HTTP API (`api/`) that a Flutter client can drive — submit a URL, get a resolved TMDB match back for human verification, trigger download+import, and poll progress — while extracting the orchestration logic into a framework-free `importer.py` module so the existing Streamlit UI (`app.py`) becomes a thin HTTP client of the same API instead of duplicating the logic.

**Architecture:** Two Python processes share one Docker image and one `einthusan_dl.py` core library:
- `api/` — FastAPI + uvicorn service. Owns all state (in-memory `JobStore`), all Einthusan/Radarr credentials, and runs the actual work on two `ThreadPoolExecutor`s (`resolve_pool`, 2 workers; `download_pool`, 1 worker — serial downloads).
- `app.py` — Streamlit UI, rewritten to hold zero business logic. It talks to `api/` exclusively through `api_client.py` (a plain `requests`-based client) and polls `GET /api/v1/jobs/{id}` to render progress.
- `importer.py` — new module holding the actual resolve → verify → download → import pipeline, extracted from `app.py::_background_import` / `_run_preview`. Both `api/worker.py` and (indirectly, via the API) `app.py` end up exercising this single code path.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, Pydantic v2, `requests`, BeautifulSoup, Playwright (unchanged), Streamlit, pytest, `httpx` (FastAPI `TestClient`), `responses` (mocking `requests` in client tests), `honcho` (local two-process dev runner).

**Spec:** `docs/superpowers/specs/2026-09-07-einthusan-http-api-design.md`

## Global Constraints

- `pytest tests/ -v` must stay green after every task — this plan is sequenced so no task leaves the suite red.
- No behavior change to the actual Einthusan scraping / Radarr API semantics — `importer.py` must reproduce `app.py::_background_import`'s current logic (tag creation, language lookup, add-or-reuse-in-Radarr, rescan+wait, download, manual-import-with-fallback-to-DownloadedMoviesScan) exactly, just reshaped as pure functions.
- Streamlit keeps zero Einthusan/Radarr credentials after the cutover — `app.py` only ever holds an API base URL + API key.
- `tmdb_id` correction via `PATCH /api/v1/jobs/{id}` is restricted to the job's own `candidates` list (422 `tmdb_not_in_candidates` otherwise) — no free-form TMDB ID input, per explicit user constraint.
- Commit after each task.

---

## Task 1: `einthusan_dl.py` — RadarrClient extensions (`monitored`, `update_movie`, `delete_movie`)

**Why:** `importer.add_to_radarr` needs to add movies **unmonitored** (Decision 6 in the spec — don't trigger Radarr's automatic search while the user is still verifying the TMDB match), then `run_download_and_import` flips `monitored=True` right before downloading. `DELETE /api/v1/jobs/{id}` needs a way to undo an unmonitored add if the user cancels before downloading.

**Files:**
- `einthusan_dl.py` (modify `RadarrClient.add_movie`, add `RadarrClient.update_movie`, add `RadarrClient.delete_movie`)
- `tests/test_radarr_import.py` (add test classes)

**Interfaces:**
```python
def add_movie(
    self,
    tmdb_result: dict,
    root_folder: str,
    quality_profile_id: int,
    language_profile_id: int,
    tags: list[int] | None = None,
    monitored: bool = True,
) -> dict: ...

def update_movie(self, movie: dict) -> dict:
    """PUT the full movie record back to Radarr (e.g. after flipping `monitored`).
    Returns the parsed JSON response."""

def delete_movie(self, movie_id: int, delete_files: bool = False) -> None:
    """DELETE a movie from Radarr's library."""
```

### Steps

1. Write failing tests in `tests/test_radarr_import.py`, appended after the existing `TestAddMovieTags` class:

```python
class TestAddMovieMonitored:
    def _make_client_with_post(self, returned_movie: dict) -> tuple[RadarrClient, MagicMock]:
        client = make_client()
        post_resp = MagicMock()
        post_resp.json.return_value = returned_movie
        client._post = MagicMock(return_value=post_resp)
        return client, client._post

    def test_add_movie_defaults_to_monitored_true(self):
        returned_movie = {"id": 42, "path": "/data/media/movies/Sabdham (2025)"}
        client, mock_post = self._make_client_with_post(returned_movie)

        client.add_movie(
            tmdb_result={"title": "Sabdham", "year": 2025, "tmdbId": 12345},
            root_folder="/data/media/movies",
            quality_profile_id=1,
            language_profile_id=1,
        )

        payload = mock_post.call_args[0][1]
        assert payload["monitored"] is True

    def test_add_movie_unmonitored_when_requested(self):
        returned_movie = {"id": 42, "path": "/data/media/movies/Sabdham (2025)"}
        client, mock_post = self._make_client_with_post(returned_movie)

        client.add_movie(
            tmdb_result={"title": "Sabdham", "year": 2025, "tmdbId": 12345},
            root_folder="/data/media/movies",
            quality_profile_id=1,
            language_profile_id=1,
            monitored=False,
        )

        payload = mock_post.call_args[0][1]
        assert payload["monitored"] is False


class TestUpdateMovie:
    def test_puts_full_movie_record_and_returns_response(self):
        client = make_client()
        put_resp = MagicMock()
        put_resp.raise_for_status = MagicMock()
        put_resp.json.return_value = {"id": 42, "monitored": True}
        client.session = MagicMock()
        client.session.put = MagicMock(return_value=put_resp)

        movie = {"id": 42, "monitored": True, "title": "Sabdham"}
        result = client.update_movie(movie)

        client.session.put.assert_called_once_with(
            "http://localhost:7878/api/v3/movie/42",
            json=movie,
            timeout=30,
        )
        assert result == {"id": 42, "monitored": True}


class TestDeleteMovie:
    def test_deletes_without_files_by_default(self):
        client = make_client()
        del_resp = MagicMock()
        del_resp.raise_for_status = MagicMock()
        client.session = MagicMock()
        client.session.delete = MagicMock(return_value=del_resp)

        client.delete_movie(42)

        client.session.delete.assert_called_once_with(
            "http://localhost:7878/api/v3/movie/42",
            params={"deleteFiles": "false"},
            timeout=30,
        )

    def test_deletes_with_files_when_requested(self):
        client = make_client()
        del_resp = MagicMock()
        del_resp.raise_for_status = MagicMock()
        client.session = MagicMock()
        client.session.delete = MagicMock(return_value=del_resp)

        client.delete_movie(42, delete_files=True)

        sent_params = client.session.delete.call_args[1]["params"]
        assert sent_params == {"deleteFiles": "true"}
```

2. Run to verify fail: `.venv/bin/pytest tests/test_radarr_import.py -v` — `TestAddMovieMonitored`, `TestUpdateMovie`, `TestDeleteMovie` fail (`monitored` always `True`; `update_movie`/`delete_movie` don't exist).

3. Implement in `einthusan_dl.py`. Change the `add_movie` signature (currently around line 810) to add the `monitored` parameter and use it in the payload instead of the hardcoded `True`:

```python
    def add_movie(
        self,
        tmdb_result: dict,
        root_folder: str,
        quality_profile_id: int,
        language_profile_id: int,
        tags: list[int] | None = None,
        monitored: bool = True,
    ) -> dict:
        """Add a movie to Radarr without triggering an automatic search."""
        payload = {
            "title": tmdb_result["title"],
            "year": tmdb_result.get("year", 0),
            "tmdbId": tmdb_result["tmdbId"],
            "qualityProfileId": quality_profile_id,
            "languageProfileId": language_profile_id,
            "rootFolderPath": root_folder,
            "monitored": monitored,
            "tags": tags or [],
            "addOptions": {
                "searchForMovie": False,
            },
        }
        log.info(f"Adding movie to Radarr: {payload['title']} ({payload['year']})")
        resp = self._post("/api/v3/movie", payload)
        movie = resp.json()
        log.info(f"Movie added. Radarr ID: {movie['id']}, folder: {movie.get('path', '?')}")
        return movie
```

   Add `update_movie` and `delete_movie` immediately after `update_movie_tags`:

```python
    def update_movie(self, movie: dict) -> dict:
        """PUT the full movie record back to Radarr (e.g. after flipping `monitored`)."""
        resp = self.session.put(
            f"{self.base}/api/v3/movie/{movie['id']}",
            json=movie,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_movie(self, movie_id: int, delete_files: bool = False) -> None:
        """DELETE a movie from Radarr's library."""
        resp = self.session.delete(
            f"{self.base}/api/v3/movie/{movie_id}",
            params={"deleteFiles": str(delete_files).lower()},
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"Deleted movie id={movie_id} from Radarr (deleteFiles={delete_files})")
```

4. Run to verify pass: `.venv/bin/pytest tests/test_radarr_import.py -v` — all green.

5. Run full suite: `.venv/bin/pytest tests/ -v` — green (requires `examples/einthusan_source.html` fixture to exist locally per `tests/test_extraction.py`; if it's missing in this environment, confirm only `test_radarr_import.py` passes and note the pre-existing collection gap — do not treat it as a regression this task introduced).

6. Commit: `fix: add monitored/update_movie/delete_movie to RadarrClient`

---

## Task 2: `importer.py` — data types, errors, `resolve_movie`

**Why:** This is the foundation both `api/worker.py` and (later) the API-driven `app.py` build on. Extracted from `app.py::_run_preview`'s login → scrape → TMDB-lookup sequence.

**Files:**
- `importer.py` (new)
- `tests/test_importer.py` (new)

**Interfaces:**
```python
class ImporterError(Exception):
    code = "internal"

class ResolveError(ImporterError):
    code = "resolve_failed"

class RadarrUnavailableError(ImporterError):
    code = "radarr_unavailable"

class DownloadError(ImporterError):
    code = "download_failed"

class DownloadCancelled(ImporterError):
    code = "cancelled"

class ImportFailedError(ImporterError):
    code = "import_failed"

@dataclass(frozen=True)
class TmdbCandidate:
    tmdb_id: int
    title: str
    year: int
    tmdb_url: str
    poster_url: str | None

@dataclass
class ResolvedMovie:
    einthusan_title: str
    einthusan_year: int
    einthusan_url: str
    video_url: str
    session: requests.Session
    candidates: list[TmdbCandidate]

EINTHUSAN_URL_RE: re.Pattern

def resolve_movie(cfg: dict, einthusan_url: str, *, on_log: Callable[[str, str], None] | None = None) -> ResolvedMovie: ...
```

### Steps

1. Write failing test file `tests/test_importer.py`:

```python
"""
Tests for importer.py — the framework-free orchestration layer shared by
api/worker.py and (via the API) app.py.

Mocks EinthusanClient and RadarrClient so no network calls are made, following
the same pattern as tests/test_radarr_import.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import importer
from importer import (
    ImportFailedError,
    ResolveError,
    ResolvedMovie,
    TmdbCandidate,
    resolve_movie,
)

CFG = {
    "einthusan": {"username": "u", "password": "p", "cookies": "", "base_url": "https://einthusan.tv"},
    "radarr": {
        "url": "http://localhost:7878",
        "api_key": "testapikey",
        "root_folder": "/data/media/movies",
        "quality_profile_id": 1,
        "language_profile_id": 1,
    },
    "staging_host": "/data/media/manual_imports",
}

TMDB_LOOKUP_RESULTS = [
    {
        "tmdbId": 111,
        "title": "Sabdham",
        "year": 2025,
        "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/poster111.jpg"}],
    },
    {
        "tmdbId": 222,
        "title": "Sabdham (Alt)",
        "year": 2025,
        "images": [],
    },
]


def _fake_einthusan_client(monkeypatch, movie_info: dict | None = None, login_error: Exception | None = None):
    fake_session = MagicMock()
    fake_client = MagicMock()
    fake_client.session = fake_session
    if login_error:
        fake_client.login.side_effect = login_error
    else:
        fake_client.login.return_value = None
    fake_client.get_movie_info.return_value = movie_info or {
        "title": "Sabdham",
        "year": 2025,
        "language": "Tamil",
        "video_url": "https://cdn1.einthusan.io/movie.mp4",
        "page_url": "https://einthusan.tv/movie/watch/abc123/",
    }
    monkeypatch.setattr(importer, "EinthusanClient", MagicMock(return_value=fake_client))
    return fake_client


def _fake_radarr_client(monkeypatch, lookup_results: list[dict] | None = None, lookup_error: Exception | None = None):
    fake_radarr = MagicMock()
    if lookup_error:
        fake_radarr.lookup_movie.side_effect = lookup_error
    else:
        fake_radarr.lookup_movie.return_value = lookup_results if lookup_results is not None else TMDB_LOOKUP_RESULTS
    monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))
    return fake_radarr


class TestEinthusanUrlRegex:
    @pytest.mark.parametrize("url", [
        "https://einthusan.tv/movie/watch/abc123/",
        "http://einthusan.tv/movie/watch/abc123",
        "https://www.einthusan.tv/premium/movie/watch/abc123/",
    ])
    def test_accepts_valid_movie_urls(self, url):
        assert importer.EINTHUSAN_URL_RE.match(url)

    @pytest.mark.parametrize("url", [
        "https://example.com/movie/watch/abc123/",
        "not a url",
        "https://einthusan.tv/",
    ])
    def test_rejects_invalid_urls(self, url):
        assert not importer.EINTHUSAN_URL_RE.match(url)


class TestResolveMovie:
    def test_returns_resolved_movie_with_candidates(self, monkeypatch):
        fake_client = _fake_einthusan_client(monkeypatch)
        _fake_radarr_client(monkeypatch)

        result = resolve_movie(CFG, "https://einthusan.tv/movie/watch/abc123/")

        assert isinstance(result, ResolvedMovie)
        assert result.einthusan_title == "Sabdham"
        assert result.einthusan_year == 2025
        assert result.video_url == "https://cdn1.einthusan.io/movie.mp4"
        assert result.session is fake_client.session
        assert len(result.candidates) == 2
        assert result.candidates[0] == TmdbCandidate(
            tmdb_id=111,
            title="Sabdham",
            year=2025,
            tmdb_url="https://www.themoviedb.org/movie/111",
            poster_url="https://image.tmdb.org/poster111.jpg",
        )
        assert result.candidates[1].poster_url is None

    def test_raises_resolve_error_on_login_failure(self, monkeypatch):
        _fake_einthusan_client(monkeypatch, login_error=RuntimeError("bad credentials"))
        _fake_radarr_client(monkeypatch)

        with pytest.raises(ResolveError):
            resolve_movie(CFG, "https://einthusan.tv/movie/watch/abc123/")

    def test_raises_resolve_error_when_no_tmdb_results(self, monkeypatch):
        _fake_einthusan_client(monkeypatch)
        _fake_radarr_client(monkeypatch, lookup_results=[])

        with pytest.raises(ResolveError):
            resolve_movie(CFG, "https://einthusan.tv/movie/watch/abc123/")

    def test_raises_radarr_unavailable_on_lookup_failure(self, monkeypatch):
        _fake_einthusan_client(monkeypatch)
        _fake_radarr_client(monkeypatch, lookup_error=RuntimeError("connection refused"))

        with pytest.raises(importer.RadarrUnavailableError):
            resolve_movie(CFG, "https://einthusan.tv/movie/watch/abc123/")

    def test_calls_on_log_callback(self, monkeypatch):
        _fake_einthusan_client(monkeypatch)
        _fake_radarr_client(monkeypatch)
        logs = []

        resolve_movie(CFG, "https://einthusan.tv/movie/watch/abc123/", on_log=lambda lvl, txt: logs.append((lvl, txt)))

        assert logs == []  # happy path logs nothing at this stage; failure paths do (see below)

    def test_on_log_called_on_failure(self, monkeypatch):
        _fake_einthusan_client(monkeypatch, login_error=RuntimeError("bad credentials"))
        _fake_radarr_client(monkeypatch)
        logs = []

        with pytest.raises(ResolveError):
            resolve_movie(CFG, "https://einthusan.tv/movie/watch/abc123/", on_log=lambda lvl, txt: logs.append((lvl, txt)))

        assert any(lvl == "ERROR" for lvl, _ in logs)
```

2. Run to verify fail: `.venv/bin/pytest tests/test_importer.py -v` — fails with `ModuleNotFoundError: No module named 'importer'`.

3. Implement `importer.py`:

```python
"""
Framework-free orchestration layer shared by the HTTP API (api/) and the
Streamlit UI (app.py, via api_client.py). Extracted from the previous
app.py::_run_preview / _background_import so both front ends drive the exact
same Einthusan -> Radarr pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from einthusan_dl import (
    EinthusanClient,
    RadarrClient,
    detect_extension,
    safe_filename,
)

log = logging.getLogger("einthusan_dl.importer")

OnLog = Callable[[str, str], None]
OnProgress = Callable[[int, int], None]

EINTHUSAN_URL_RE = re.compile(
    r"^https?://(?:www\.)?einthusan\.tv/(?:premium/)?movie/watch/[^/]+/?$",
    re.IGNORECASE,
)


# ── Errors ────────────────────────────────────────────────────────────────

class ImporterError(Exception):
    """Base class for all importer failures. Always carries a stable `code`
    used by api/worker.py to populate a job's `error.code`."""
    code = "internal"


class ResolveError(ImporterError):
    code = "resolve_failed"


class RadarrUnavailableError(ImporterError):
    code = "radarr_unavailable"


class DownloadError(ImporterError):
    code = "download_failed"


class DownloadCancelled(ImporterError):
    code = "cancelled"


class ImportFailedError(ImporterError):
    code = "import_failed"


# ── Data ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TmdbCandidate:
    tmdb_id: int
    title: str
    year: int
    tmdb_url: str
    poster_url: str | None


@dataclass
class ResolvedMovie:
    einthusan_title: str
    einthusan_year: int
    einthusan_url: str
    video_url: str
    session: requests.Session
    candidates: list[TmdbCandidate]


def _log(on_log: OnLog | None, level: str, text: str) -> None:
    if on_log:
        on_log(level, text)
    getattr(log, level.lower(), log.info)(text)


def _poster_url(tmdb_result: dict) -> str | None:
    for image in tmdb_result.get("images", []) or []:
        if image.get("coverType") == "poster":
            return image.get("remoteUrl") or image.get("url")
    return None


def _to_candidate(tmdb_result: dict) -> TmdbCandidate:
    tmdb_id = tmdb_result["tmdbId"]
    return TmdbCandidate(
        tmdb_id=tmdb_id,
        title=tmdb_result["title"],
        year=tmdb_result.get("year", 0),
        tmdb_url=f"https://www.themoviedb.org/movie/{tmdb_id}",
        poster_url=_poster_url(tmdb_result),
    )


# ── Resolve ───────────────────────────────────────────────────────────────

def resolve_movie(cfg: dict, einthusan_url: str, *, on_log: OnLog | None = None) -> ResolvedMovie:
    """Log in to Einthusan, scrape the movie page, and look up TMDB candidates
    via Radarr. Raises ResolveError or RadarrUnavailableError on failure."""
    try:
        client = EinthusanClient(
            cfg["einthusan"]["username"],
            cfg["einthusan"]["password"],
            cfg["einthusan"]["cookies"],
        )
        client.login()
        movie_info = client.get_movie_info(einthusan_url)
    except Exception as exc:
        _log(on_log, "ERROR", f"Failed to resolve Einthusan movie: {exc}")
        raise ResolveError(str(exc)) from exc

    radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
    try:
        results = radarr.lookup_movie(movie_info["title"], movie_info["year"])
    except Exception as exc:
        _log(on_log, "ERROR", f"Radarr TMDB lookup failed: {exc}")
        raise RadarrUnavailableError(str(exc)) from exc

    if not results:
        message = f"No TMDB results found for '{movie_info['title']}' ({movie_info['year']})"
        _log(on_log, "ERROR", message)
        raise ResolveError(message)

    candidates = [_to_candidate(r) for r in results[:5]]

    return ResolvedMovie(
        einthusan_title=movie_info["title"],
        einthusan_year=movie_info["year"],
        einthusan_url=einthusan_url,
        video_url=movie_info["video_url"],
        session=client.session,
        candidates=candidates,
    )
```

4. Run to verify pass: `.venv/bin/pytest tests/test_importer.py -v` — all green.

5. Run full suite: `.venv/bin/pytest tests/ -v` — green.

6. Commit: `feat: add importer.py with resolve_movie`

---

## Task 3: `importer.py` — `add_to_radarr`, `remove_from_radarr`

**Why:** Splits the "add/reuse movie in Radarr, tag it" step (used at job-creation-verified time, i.e. right before `POST /jobs/{id}/download`) from the download/import step, and adds the cleanup path `DELETE /jobs/{id}` needs.

**Files:**
- `importer.py` (extend)
- `tests/test_importer.py` (extend)

**Interfaces:**
```python
def add_to_radarr(cfg: dict, candidate: TmdbCandidate, *, monitored: bool = False, on_log: OnLog | None = None) -> int: ...
def remove_from_radarr(cfg: dict, radarr_movie_id: int, *, delete_files: bool = False, on_log: OnLog | None = None) -> None: ...
```

### Steps

1. Add failing tests to `tests/test_importer.py`:

```python
from importer import add_to_radarr, remove_from_radarr

SABDHAM_CANDIDATE = TmdbCandidate(
    tmdb_id=111,
    title="Sabdham",
    year=2025,
    tmdb_url="https://www.themoviedb.org/movie/111",
    poster_url=None,
)


class TestAddToRadarr:
    def test_adds_new_movie_unmonitored_by_default(self, monkeypatch):
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.return_value = 5
        fake_radarr.get_existing_movie.return_value = None
        fake_radarr.add_movie.return_value = {"id": 42}
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        movie_id = add_to_radarr(CFG, SABDHAM_CANDIDATE)

        assert movie_id == 42
        fake_radarr.add_movie.assert_called_once()
        _, kwargs = fake_radarr.add_movie.call_args
        assert kwargs["monitored"] is False
        assert kwargs["tags"] == [5]

    def test_can_add_monitored_when_requested(self, monkeypatch):
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.return_value = 5
        fake_radarr.get_existing_movie.return_value = None
        fake_radarr.add_movie.return_value = {"id": 42}
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        add_to_radarr(CFG, SABDHAM_CANDIDATE, monitored=True)

        _, kwargs = fake_radarr.add_movie.call_args
        assert kwargs["monitored"] is True

    def test_reuses_existing_movie_and_tags_it(self, monkeypatch):
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.return_value = 5
        fake_radarr.get_existing_movie.return_value = {"id": 99, "tags": []}
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        movie_id = add_to_radarr(CFG, SABDHAM_CANDIDATE)

        assert movie_id == 99
        fake_radarr.add_movie.assert_not_called()
        fake_radarr.update_movie_tags.assert_called_once_with({"id": 99, "tags": []}, [5])

    def test_raises_radarr_unavailable_on_failure(self, monkeypatch):
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        with pytest.raises(importer.RadarrUnavailableError):
            add_to_radarr(CFG, SABDHAM_CANDIDATE)


class TestRemoveFromRadarr:
    def test_deletes_movie_without_files_by_default(self, monkeypatch):
        fake_radarr = MagicMock()
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        remove_from_radarr(CFG, 42)

        fake_radarr.delete_movie.assert_called_once_with(42, delete_files=False)

    def test_deletes_movie_with_files_when_requested(self, monkeypatch):
        fake_radarr = MagicMock()
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        remove_from_radarr(CFG, 42, delete_files=True)

        fake_radarr.delete_movie.assert_called_once_with(42, delete_files=True)

    def test_raises_radarr_unavailable_on_failure(self, monkeypatch):
        fake_radarr = MagicMock()
        fake_radarr.delete_movie.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        with pytest.raises(importer.RadarrUnavailableError):
            remove_from_radarr(CFG, 42)
```

2. Run to verify fail: `.venv/bin/pytest tests/test_importer.py -v` — `add_to_radarr`/`remove_from_radarr` don't exist yet.

3. Append to `importer.py`:

```python
# ── Add / remove in Radarr ───────────────────────────────────────────────

def add_to_radarr(
    cfg: dict,
    candidate: TmdbCandidate,
    *,
    monitored: bool = False,
    on_log: OnLog | None = None,
) -> int:
    """Add (or reuse + tag) the given TMDB candidate in Radarr.
    Returns the Radarr movie ID. Defaults to unmonitored so no automatic
    search fires before the user has verified the match."""
    radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
    try:
        tag_id = radarr.get_or_create_tag("einthusan")
        existing = radarr.get_existing_movie(candidate.tmdb_id)
        if existing:
            _log(on_log, "INFO", f"Movie already in Radarr (id={existing['id']}): {candidate.title}")
            radarr.update_movie_tags(existing, [tag_id])
            return existing["id"]

        _log(on_log, "INFO", f"Adding '{candidate.title} ({candidate.year})' to Radarr …")
        movie = radarr.add_movie(
            {"title": candidate.title, "year": candidate.year, "tmdbId": candidate.tmdb_id},
            cfg["radarr"]["root_folder"],
            cfg["radarr"]["quality_profile_id"],
            cfg["radarr"]["language_profile_id"],
            tags=[tag_id],
            monitored=monitored,
        )
        return movie["id"]
    except Exception as exc:
        _log(on_log, "ERROR", f"Failed to add movie to Radarr: {exc}")
        raise RadarrUnavailableError(str(exc)) from exc


def remove_from_radarr(
    cfg: dict,
    radarr_movie_id: int,
    *,
    delete_files: bool = False,
    on_log: OnLog | None = None,
) -> None:
    """Undo add_to_radarr — used when a job is cancelled/deleted before
    downloading (e.g. the user rejected all candidates)."""
    radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
    try:
        radarr.delete_movie(radarr_movie_id, delete_files=delete_files)
        _log(on_log, "INFO", f"Removed movie id={radarr_movie_id} from Radarr")
    except Exception as exc:
        _log(on_log, "ERROR", f"Failed to remove movie from Radarr: {exc}")
        raise RadarrUnavailableError(str(exc)) from exc
```

4. Run to verify pass: `.venv/bin/pytest tests/test_importer.py -v` — all green.

5. Commit: `feat: add importer.add_to_radarr and remove_from_radarr`

---

## Task 4: `importer.py` — `run_download_and_import`

**Why:** The full download + manual-import pipeline, extracted from `app.py::_background_import`'s post-add-movie logic, with a single automatic retry (fresh Einthusan session) if the CDN URL has expired, and cooperative cancellation via `DownloadCancelled` raised from inside `on_progress`.

**Files:**
- `importer.py` (extend)
- `tests/test_importer.py` (extend)

**Interfaces:**
```python
def run_download_and_import(
    cfg: dict,
    *,
    resolved: ResolvedMovie,
    candidate: TmdbCandidate,
    radarr_movie_id: int,
    on_progress: OnProgress | None = None,
    on_log: OnLog | None = None,
) -> Path: ...
```

### Steps

1. Add failing tests to `tests/test_importer.py`:

```python
from importer import run_download_and_import

def _resolved_movie(session=None):
    return ResolvedMovie(
        einthusan_title="Sabdham",
        einthusan_year=2025,
        einthusan_url="https://einthusan.tv/movie/watch/abc123/",
        video_url="https://cdn1.einthusan.io/movie.mp4",
        session=session or MagicMock(),
        candidates=[SABDHAM_CANDIDATE],
    )


class TestRunDownloadAndImport:
    def _fake_radarr(self, monkeypatch, *, existing_movie=None, import_items=None):
        fake_radarr = MagicMock()
        fake_radarr.get_existing_movie.return_value = existing_movie or {"id": 42, "monitored": False, "tmdbId": 111}
        fake_radarr.rescan_movie.return_value = 1
        fake_radarr.wait_for_command.return_value = True
        fake_radarr.get_language_id.return_value = 11
        fake_radarr.manual_import_analyze.return_value = (
            import_items if import_items is not None else [{"id": 9, "path": "/data/media/manual_imports/Sabdham (2025).mp4", "movie": {"id": 42}, "quality": {}}]
        )
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))
        return fake_radarr

    def _fake_einthusan_new(self, monkeypatch, download_side_effect=None):
        fake_client = MagicMock()
        if download_side_effect:
            fake_client.download.side_effect = download_side_effect
        else:
            fake_client.download.return_value = Path("/data/media/manual_imports/Sabdham (2025).mp4")
        monkeypatch.setattr(
            importer.EinthusanClient, "__new__", MagicMock(return_value=fake_client)
        )
        return fake_client

    def test_happy_path_downloads_and_imports(self, monkeypatch):
        fake_radarr = self._fake_radarr(monkeypatch)
        fake_client = self._fake_einthusan_new(monkeypatch)

        dest = run_download_and_import(
            CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42,
        )

        assert dest.name == "Sabdham (2025).mp4"
        fake_client.download.assert_called_once()
        fake_radarr.manual_import_approve.assert_called_once()

    def test_flips_monitored_true_before_downloading(self, monkeypatch):
        fake_radarr = self._fake_radarr(monkeypatch, existing_movie={"id": 42, "monitored": False, "tmdbId": 111})
        self._fake_einthusan_new(monkeypatch)

        run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

        fake_radarr.update_movie.assert_called_once()
        sent_movie = fake_radarr.update_movie.call_args[0][0]
        assert sent_movie["monitored"] is True

    def test_does_not_repatch_if_already_monitored(self, monkeypatch):
        fake_radarr = self._fake_radarr(monkeypatch, existing_movie={"id": 42, "monitored": True, "tmdbId": 111})
        self._fake_einthusan_new(monkeypatch)

        run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

        fake_radarr.update_movie.assert_not_called()

    def test_raises_import_failed_when_no_match(self, monkeypatch):
        self._fake_radarr(monkeypatch, import_items=[])
        self._fake_einthusan_new(monkeypatch)

        with pytest.raises(ImportFailedError):
            run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

    def test_retries_download_once_with_fresh_session_on_failure(self, monkeypatch):
        fake_radarr = self._fake_radarr(monkeypatch)
        fresh_resolved = _resolved_movie()
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=fresh_resolved))

        call_count = {"n": 0}

        def flaky_download(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("403 Forbidden")
            return Path("/data/media/manual_imports/Sabdham (2025).mp4")

        self._fake_einthusan_new(monkeypatch, download_side_effect=flaky_download)

        dest = run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

        assert call_count["n"] == 2
        assert dest.name == "Sabdham (2025).mp4"

    def test_raises_download_error_when_retry_also_fails(self, monkeypatch):
        self._fake_radarr(monkeypatch)
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=_resolved_movie()))
        self._fake_einthusan_new(monkeypatch, download_side_effect=RuntimeError("403 Forbidden"))

        with pytest.raises(DownloadError):
            run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

    def test_propagates_download_cancelled_without_retry(self, monkeypatch):
        self._fake_radarr(monkeypatch)
        resolve_spy = MagicMock()
        monkeypatch.setattr(importer, "resolve_movie", resolve_spy)
        self._fake_einthusan_new(monkeypatch, download_side_effect=importer.DownloadCancelled("stopped"))

        with pytest.raises(importer.DownloadCancelled):
            run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

        resolve_spy.assert_not_called()
```

2. Run to verify fail: `.venv/bin/pytest tests/test_importer.py -v` — `run_download_and_import` doesn't exist.

3. Append to `importer.py` (also add `from pathlib import Path` — already imported above):

```python
# ── Download + import ────────────────────────────────────────────────────

def run_download_and_import(
    cfg: dict,
    *,
    resolved: ResolvedMovie,
    candidate: TmdbCandidate,
    radarr_movie_id: int,
    on_progress: OnProgress | None = None,
    on_log: OnLog | None = None,
) -> Path:
    """Download the resolved video and hand it to Radarr's manual import.

    On a download failure that isn't a cancellation, re-resolves the
    Einthusan session once (the CDN-signed URL may have expired) and retries
    the download exactly once before giving up.
    """
    radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])

    # Flip monitored=True now that the user has verified the match.
    try:
        movie = radarr.get_existing_movie(candidate.tmdb_id)
        if movie and not movie.get("monitored"):
            movie["monitored"] = True
            radarr.update_movie(movie)
    except Exception as exc:
        _log(on_log, "WARNING", f"Could not set movie monitored: {exc}")

    _log(on_log, "INFO", "Waiting for Radarr to create the movie folder …")
    cmd_id = radarr.rescan_movie(radarr_movie_id)
    if cmd_id:
        radarr.wait_for_command(cmd_id, timeout=30)

    ext = detect_extension(resolved.video_url)
    filename = safe_filename(candidate.title, candidate.year, ext)
    dest_path = Path(cfg["staging_host"]) / filename

    _log(on_log, "INFO", f"Downloading to: {dest_path}")
    _download_with_retry(cfg, resolved, dest_path, on_progress=on_progress, on_log=on_log)

    _manual_import(cfg, radarr, radarr_movie_id, dest_path, on_log=on_log)

    return dest_path


def _download_with_retry(
    cfg: dict,
    resolved: ResolvedMovie,
    dest_path: Path,
    *,
    on_progress: OnProgress | None,
    on_log: OnLog | None,
) -> None:
    client = EinthusanClient.__new__(EinthusanClient)
    client.session = resolved.session
    try:
        client.download(resolved.video_url, dest_path, on_progress=on_progress)
        return
    except DownloadCancelled:
        raise
    except Exception as exc:
        _log(on_log, "WARNING", f"Download failed ({exc}); re-resolving Einthusan session and retrying once …")

    try:
        fresh = resolve_movie(cfg, resolved.einthusan_url, on_log=on_log)
    except ResolveError as re_exc:
        raise DownloadError(f"Download failed and re-resolve also failed: {re_exc}") from re_exc

    retry_client = EinthusanClient.__new__(EinthusanClient)
    retry_client.session = fresh.session
    try:
        retry_client.download(fresh.video_url, dest_path, on_progress=on_progress)
    except DownloadCancelled:
        raise
    except Exception as retry_exc:
        raise DownloadError(str(retry_exc)) from retry_exc


def _manual_import(cfg: dict, radarr: RadarrClient, radarr_movie_id: int, dest_path: Path, *, on_log: OnLog | None) -> None:
    radarr_file_path = str(Path(cfg["staging_host"]) / dest_path.name)
    try:
        tamil_lang_id = radarr.get_language_id("Tamil")
        _log(on_log, "INFO", "Asking Radarr to analyse the staging folder …")
        import_items = radarr.manual_import_analyze(cfg["staging_host"], radarr_movie_id)
        matching = [i for i in import_items if Path(i["path"]).name == dest_path.name]

        if not matching:
            all_paths = [i.get("path", "") for i in import_items]
            raise ImportFailedError(
                f"Radarr could not see '{dest_path.name}' in the staging folder. "
                f"Files Radarr did see: {all_paths or 'none'}"
            )

        radarr.manual_import_approve(
            matching,
            language_id=tamil_lang_id,
            language_name="Tamil",
            release_group="einthusan",
            movie_id=radarr_movie_id,
        )
        _log(on_log, "INFO", "Import submitted to Radarr ✓")
        cmd_id = radarr.rescan_movie(radarr_movie_id)
        if cmd_id:
            radarr.wait_for_command(cmd_id, timeout=60)
    except ImportFailedError:
        raise
    except Exception as imp_exc:
        _log(on_log, "WARNING", f"Manual import API failed ({imp_exc}). Falling back to DownloadedMoviesScan …")
        try:
            radarr.downloaded_movies_scan(radarr_file_path)
            cmd_id = radarr.rescan_movie(radarr_movie_id)
            if cmd_id:
                radarr.wait_for_command(cmd_id, timeout=60)
        except Exception as scan_exc:
            raise ImportFailedError(f"Both manual import and DownloadedMoviesScan failed: {scan_exc}") from scan_exc
```

   Note: `test_raises_import_failed_when_no_match` expects `ImportFailedError` to propagate for the "no matching file" case specifically (not silently fall back — there's nothing to fall back to if Radarr didn't even see the file), which is exactly what the `if not matching: raise ImportFailedError(...)` branch does, caught immediately by the `except ImportFailedError: raise` re-raise.

4. Run to verify pass: `.venv/bin/pytest tests/test_importer.py -v` — all green.

5. Run full suite: `.venv/bin/pytest tests/ -v` — green.

6. Commit: `feat: add importer.run_download_and_import with retry and cancellation support`

---

## Task 5: `api/settings.py` — settings loader

**Why:** Needs to be injectable (`core_config` param) so `test_api.py` doesn't need a real `.env` file, per the design refinement in the spec review.

**Files:**
- `api/__init__.py` (new, empty module docstring only — side-effect-free)
- `api/settings.py` (new)
- `tests/test_api_settings.py` (new)

**Interfaces:**
```python
@dataclass(frozen=True)
class Settings:
    api_key: str
    core_config: dict

def load_settings(core_config: dict | None = None) -> Settings: ...
def get_settings() -> Settings: ...  # lru_cache-wrapped, for __main__.py
```

### Steps

1. Create `api/__init__.py`:

```python
"""HTTP API for einthusan-downloader. See docs/superpowers/specs/2026-09-07-einthusan-http-api-design.md."""
```

2. Write failing test `tests/test_api_settings.py`:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.settings import load_settings


def test_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("EINTHUSAN_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="EINTHUSAN_API_KEY"):
        load_settings(core_config={})


def test_uses_injected_core_config(monkeypatch):
    monkeypatch.setenv("EINTHUSAN_API_KEY", "secret123")
    fake_cfg = {"radarr": {"url": "http://localhost:7878"}}

    settings = load_settings(core_config=fake_cfg)

    assert settings.api_key == "secret123"
    assert settings.core_config is fake_cfg


def test_loads_core_config_from_env_when_not_injected(monkeypatch, tmp_path):
    monkeypatch.setenv("EINTHUSAN_API_KEY", "secret123")
    import api.settings as settings_mod
    monkeypatch.setattr(settings_mod, "load_config", lambda: {"radarr": {"url": "http://real"}})

    settings = load_settings()

    assert settings.core_config == {"radarr": {"url": "http://real"}}
```

3. Run to verify fail: `.venv/bin/pytest tests/test_api_settings.py -v` — `ModuleNotFoundError: No module named 'api.settings'`.

4. Implement `api/settings.py`:

```python
"""Load and validate configuration for the FastAPI service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from einthusan_dl import load_config


@dataclass(frozen=True)
class Settings:
    api_key: str
    core_config: dict


def load_settings(core_config: dict | None = None) -> Settings:
    """Build Settings for the API.

    `core_config` is injectable so tests don't need a real .env file;
    production callers omit it and it's loaded from einthusan_dl.load_config().
    """
    api_key = os.environ.get("EINTHUSAN_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "EINTHUSAN_API_KEY is not set. Set it in .env or the environment "
            "before starting the API server."
        )
    resolved_config = core_config if core_config is not None else load_config()
    return Settings(api_key=api_key, core_config=resolved_config)


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor for production use (api/__main__.py)."""
    return load_settings()
```

5. Run to verify pass: `.venv/bin/pytest tests/test_api_settings.py -v` — green.

6. Commit: `feat: add api/settings.py`

---

## Task 6: `api/jobs.py` — job state and in-memory store

**Files:**
- `api/jobs.py` (new)
- `tests/test_api_jobs.py` (new)

**Interfaces:**
```python
JobState = Literal["resolving", "resolve_failed", "awaiting_verification", "downloading", "importing", "done", "error"]

@dataclass
class Progress:
    downloaded_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: float | None = None

@dataclass
class Job:
    id: str
    einthusan_url: str
    state: JobState = "resolving"
    created_at: float
    updated_at: float
    candidates: list[TmdbCandidate] = field(default_factory=list)
    selected_tmdb_id: int | None = None
    resolved: ResolvedMovie | None = None
    radarr_movie_id: int | None = None
    progress: Progress = field(default_factory=Progress)
    result: dict | None = None
    error: dict | None = None
    cancelled: bool = False

class JobStore:
    def create(self, einthusan_url: str) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def list(self) -> list[Job]: ...
    def delete(self, job_id: str) -> Job | None: ...
    def update(self, job_id: str, **fields) -> Job | None: ...
```

### Steps

1. Write failing test `tests/test_api_jobs.py`:

```python
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.jobs import Job, JobStore, Progress


class TestJobStore:
    def test_create_returns_job_with_uuid_id_and_resolving_state(self):
        store = JobStore()

        job = store.create("https://einthusan.tv/movie/watch/abc123/")

        assert job.einthusan_url == "https://einthusan.tv/movie/watch/abc123/"
        assert job.state == "resolving"
        assert len(job.id) == 36  # uuid4 string

    def test_get_returns_none_for_unknown_id(self):
        store = JobStore()
        assert store.get("nonexistent") is None

    def test_get_returns_created_job(self):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc123/")

        assert store.get(job.id) is job

    def test_list_returns_all_jobs(self):
        store = JobStore()
        j1 = store.create("https://einthusan.tv/movie/watch/a/")
        j2 = store.create("https://einthusan.tv/movie/watch/b/")

        assert {j.id for j in store.list()} == {j1.id, j2.id}

    def test_update_sets_fields_and_bumps_updated_at(self):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc123/")
        original_updated_at = job.updated_at

        updated = store.update(job.id, state="awaiting_verification", selected_tmdb_id=111)

        assert updated.state == "awaiting_verification"
        assert updated.selected_tmdb_id == 111
        assert updated.updated_at >= original_updated_at

    def test_update_returns_none_for_unknown_id(self):
        store = JobStore()
        assert store.update("nonexistent", state="done") is None

    def test_delete_removes_and_returns_job(self):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc123/")

        deleted = store.delete(job.id)

        assert deleted.id == job.id
        assert store.get(job.id) is None

    def test_delete_returns_none_for_unknown_id(self):
        store = JobStore()
        assert store.delete("nonexistent") is None

    def test_concurrent_creates_are_thread_safe(self):
        store = JobStore()
        ids: list[str] = []
        lock = threading.Lock()

        def worker():
            job = store.create("https://einthusan.tv/movie/watch/abc123/")
            with lock:
                ids.append(job.id)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(ids)) == 20
        assert len(store.list()) == 20


class TestProgressDefaults:
    def test_defaults_to_zero(self):
        p = Progress()
        assert p.downloaded_bytes == 0
        assert p.total_bytes == 0
        assert p.eta_seconds is None
```

2. Run to verify fail: `.venv/bin/pytest tests/test_api_jobs.py -v` — module doesn't exist.

3. Implement `api/jobs.py`:

```python
"""In-memory job store for the movie import workflow."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from importer import ResolvedMovie, TmdbCandidate

JobState = Literal[
    "resolving",
    "resolve_failed",
    "awaiting_verification",
    "downloading",
    "importing",
    "done",
    "error",
]


@dataclass
class Progress:
    downloaded_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: float | None = None


@dataclass
class Job:
    id: str
    einthusan_url: str
    state: JobState = "resolving"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    candidates: list[TmdbCandidate] = field(default_factory=list)
    selected_tmdb_id: int | None = None
    resolved: ResolvedMovie | None = None
    radarr_movie_id: int | None = None
    progress: Progress = field(default_factory=Progress)
    result: dict | None = None
    error: dict | None = None
    cancelled: bool = False


class JobStore:
    """Thread-safe in-memory job registry. Not persisted — restarting the API
    process loses all jobs, per the spec's explicit in-memory-store decision."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def create(self, einthusan_url: str) -> Job:
        job = Job(id=str(uuid.uuid4()), einthusan_url=einthusan_url)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def delete(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.pop(job_id, None)

    def update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            return job
```

4. Run to verify pass: `.venv/bin/pytest tests/test_api_jobs.py -v` — green.

5. Commit: `feat: add api/jobs.py with thread-safe JobStore`

---

## Task 7: `api/worker.py` — resolve and download thread pools

**Files:**
- `api/worker.py` (new)
- `tests/test_api_worker.py` (new)

**Interfaces:**
```python
resolve_pool: ThreadPoolExecutor  # 2 workers
download_pool: ThreadPoolExecutor  # 1 worker, serial

def submit_resolve(store: JobStore, core_config: dict, job: Job) -> None: ...
def submit_download(store: JobStore, core_config: dict, job: Job) -> None: ...
```

### Steps

1. Write failing test `tests/test_api_worker.py`. Tests call the module-level `_run_resolve`/`_run_download` functions directly (synchronously) rather than going through the thread pools, so they're deterministic:

```python
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import importer
from api.jobs import JobStore, Progress
from api import worker

CFG = {"radarr": {"url": "http://localhost:7878", "api_key": "k"}, "staging_host": "/staging"}


def _candidate(tmdb_id=111):
    return importer.TmdbCandidate(tmdb_id=tmdb_id, title="Sabdham", year=2025, tmdb_url="https://tmdb/111", poster_url=None)


class TestRunResolve:
    def test_success_sets_awaiting_verification_with_candidates(self, monkeypatch):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc/")
        resolved = importer.ResolvedMovie(
            einthusan_title="Sabdham", einthusan_year=2025, einthusan_url=job.einthusan_url,
            video_url="https://cdn/movie.mp4", session=MagicMock(), candidates=[_candidate()],
        )
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=resolved))

        worker._run_resolve(store, CFG, job.id)

        updated = store.get(job.id)
        assert updated.state == "awaiting_verification"
        assert updated.candidates == [_candidate()]
        assert updated.selected_tmdb_id == 111

    def test_resolve_error_sets_resolve_failed_with_code(self, monkeypatch):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc/")
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(side_effect=importer.ResolveError("no results")))

        worker._run_resolve(store, CFG, job.id)

        updated = store.get(job.id)
        assert updated.state == "resolve_failed"
        assert updated.error == {"code": "resolve_failed", "message": "no results"}

    def test_unexpected_exception_also_sets_resolve_failed(self, monkeypatch):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc/")
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(side_effect=RuntimeError("boom")))

        worker._run_resolve(store, CFG, job.id)

        updated = store.get(job.id)
        assert updated.state == "resolve_failed"
        assert updated.error["code"] == "internal"


class TestRunDownload:
    def _job_ready_for_download(self, store):
        job = store.create("https://einthusan.tv/movie/watch/abc/")
        resolved = importer.ResolvedMovie(
            einthusan_title="Sabdham", einthusan_year=2025, einthusan_url=job.einthusan_url,
            video_url="https://cdn/movie.mp4", session=MagicMock(), candidates=[_candidate()],
        )
        store.update(job.id, state="awaiting_verification", candidates=[_candidate()],
                     selected_tmdb_id=111, resolved=resolved, radarr_movie_id=42)
        return job.id

    def test_success_sets_done_with_result(self, monkeypatch):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        monkeypatch.setattr(importer, "run_download_and_import", MagicMock(return_value=Path("/staging/Sabdham (2025).mp4")))

        worker._run_download(store, CFG, job_id)

        updated = store.get(job_id)
        assert updated.state == "done"
        assert updated.result == {"file": "/staging/Sabdham (2025).mp4", "radarr_movie_id": 42, "tmdb_id": 111}

    def test_importer_error_sets_error_state_with_code(self, monkeypatch):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        monkeypatch.setattr(importer, "run_download_and_import", MagicMock(side_effect=importer.DownloadError("timed out")))

        worker._run_download(store, CFG, job_id)

        updated = store.get(job_id)
        assert updated.state == "error"
        assert updated.error == {"code": "download_failed", "message": "timed out"}

    def test_cancelled_sets_error_with_cancelled_code(self, monkeypatch):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        monkeypatch.setattr(importer, "run_download_and_import", MagicMock(side_effect=importer.DownloadCancelled("stopped")))

        worker._run_download(store, CFG, job_id)

        updated = store.get(job_id)
        assert updated.state == "error"
        assert updated.error["code"] == "cancelled"

    def test_progress_callback_updates_progress_and_flips_to_importing_at_100_percent(self):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        cb = worker._make_progress_callback(store, job_id)

        cb(50, 100)
        mid = store.get(job_id)
        assert mid.progress.total_bytes == 100
        assert mid.state == "downloading"

        cb(100, 100)
        after = store.get(job_id)
        assert after.state == "importing"

    def test_progress_callback_raises_when_job_cancelled(self):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        store.update(job_id, cancelled=True)
        cb = worker._make_progress_callback(store, job_id)

        with pytest.raises(importer.DownloadCancelled):
            cb(10, 100)
```

   Add `from pathlib import Path` alongside this test file's other imports at the top.

2. Run to verify fail: `.venv/bin/pytest tests/test_api_worker.py -v` — module doesn't exist.

3. Implement `api/worker.py`:

```python
"""Background execution: resolve and download/import thread pools driving importer.py."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import importer
from api.jobs import Job, JobStore, Progress

resolve_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resolve")
download_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")

_PROGRESS_SAMPLE_SECONDS = 0.5


def submit_resolve(store: JobStore, core_config: dict, job: Job) -> None:
    resolve_pool.submit(_run_resolve, store, core_config, job.id)


def _run_resolve(store: JobStore, core_config: dict, job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        return
    try:
        resolved = importer.resolve_movie(core_config, job.einthusan_url)
    except importer.ImporterError as exc:
        store.update(job_id, state="resolve_failed", error={"code": exc.code, "message": str(exc)})
        return
    except Exception as exc:
        store.update(job_id, state="resolve_failed", error={"code": "internal", "message": str(exc)})
        return

    store.update(
        job_id,
        state="awaiting_verification",
        resolved=resolved,
        candidates=resolved.candidates,
        selected_tmdb_id=resolved.candidates[0].tmdb_id if resolved.candidates else None,
    )


def submit_download(store: JobStore, core_config: dict, job: Job) -> None:
    download_pool.submit(_run_download, store, core_config, job.id)


def _make_progress_callback(store: JobStore, job_id: str):
    state = {"last_ts": time.time(), "last_bytes": 0}

    def on_progress(downloaded: int, total: int) -> None:
        job = store.get(job_id)
        if job is None:
            return
        if job.cancelled:
            raise importer.DownloadCancelled("Job was cancelled")

        now = time.time()
        elapsed = now - state["last_ts"]
        if elapsed < _PROGRESS_SAMPLE_SECONDS and downloaded < total:
            return

        delta_bytes = downloaded - state["last_bytes"]
        speed_bps = delta_bytes / elapsed if elapsed > 0 else 0.0
        remaining = max(total - downloaded, 0)
        eta_seconds = remaining / speed_bps if speed_bps > 0 else None
        percent = (downloaded / total * 100) if total else 0.0

        new_state = "importing" if total and downloaded >= total else "downloading"
        store.update(
            job_id,
            state=new_state,
            progress=Progress(
                downloaded_bytes=downloaded,
                total_bytes=total,
                percent=round(percent, 2),
                speed_bps=round(speed_bps, 2),
                eta_seconds=round(eta_seconds, 1) if eta_seconds is not None else None,
            ),
        )
        state["last_ts"] = now
        state["last_bytes"] = downloaded

    return on_progress


def _run_download(store: JobStore, core_config: dict, job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        return

    store.update(job_id, state="downloading")
    candidate = next((c for c in job.candidates if c.tmdb_id == job.selected_tmdb_id), None)
    if candidate is None:
        store.update(job_id, state="error", error={"code": "internal", "message": "No candidate selected"})
        return

    on_progress = _make_progress_callback(store, job_id)
    try:
        dest = importer.run_download_and_import(
            core_config,
            resolved=job.resolved,
            candidate=candidate,
            radarr_movie_id=job.radarr_movie_id,
            on_progress=on_progress,
        )
    except importer.DownloadCancelled:
        store.update(job_id, state="error", error={"code": "cancelled", "message": "Job was cancelled"})
        return
    except importer.ImporterError as exc:
        store.update(job_id, state="error", error={"code": exc.code, "message": str(exc)})
        return
    except Exception as exc:
        store.update(job_id, state="error", error={"code": "internal", "message": str(exc)})
        return

    store.update(
        job_id,
        state="done",
        result={"file": str(dest), "radarr_movie_id": job.radarr_movie_id, "tmdb_id": candidate.tmdb_id},
    )
```

4. Run to verify pass: `.venv/bin/pytest tests/test_api_worker.py -v` — all green.

5. Run full suite: `.venv/bin/pytest tests/ -v` — green.

6. Commit: `feat: add api/worker.py with resolve/download thread pools`

---

## Task 8: `api/auth.py`, `api/models.py`, `api/routes.py`, `api/main.py`, `api/__main__.py`

**Why:** The actual HTTP surface. One task because the routes, models, and app factory are tightly coupled and are best verified together via `TestClient`.

**Files:**
- `api/auth.py` (new)
- `api/models.py` (new)
- `api/routes.py` (new)
- `api/main.py` (new)
- `api/__main__.py` (new)
- `tests/test_api.py` (new)
- `requirements.txt` (add `fastapi`, `uvicorn[standard]`, `httpx`, `responses`, `honcho`)

### Steps

1. Add to `requirements.txt`:

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
httpx>=0.27.0
responses>=0.25.0
honcho>=1.1.0
```

   Then: `.venv/bin/uv pip install -r requirements.txt` (or re-run `bash setup.sh`) to install the new deps before writing tests that import `fastapi`.

2. Write failing test `tests/test_api.py`:

```python
"""
Integration tests for the FastAPI app, using TestClient. importer functions
are monkeypatched at the module-attribute level (importer.resolve_movie, etc.)
so api/worker.py's `import importer; importer.resolve_movie(...)` picks up the
patched version.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import importer
from api.main import create_app
from api.settings import Settings

CORE_CONFIG = {
    "einthusan": {"username": "u", "password": "p", "cookies": "", "base_url": "https://einthusan.tv"},
    "radarr": {"url": "http://localhost:7878", "api_key": "k", "root_folder": "/movies", "quality_profile_id": 1, "language_profile_id": 1},
    "staging_host": "/staging",
}

API_KEY = "test-api-key"


@pytest.fixture
def client():
    settings = Settings(api_key=API_KEY, core_config=CORE_CONFIG)
    app = create_app(settings=settings)
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-Api-Key": API_KEY}


def _candidate(tmdb_id=111, title="Sabdham"):
    return importer.TmdbCandidate(tmdb_id=tmdb_id, title=title, year=2025, tmdb_url=f"https://tmdb/{tmdb_id}", poster_url=None)


def _resolved(url):
    return importer.ResolvedMovie(
        einthusan_title="Sabdham", einthusan_year=2025, einthusan_url=url,
        video_url="https://cdn/movie.mp4", session=MagicMock(), candidates=[_candidate(), _candidate(222, "Sabdham Alt")],
    )


def _wait_for_state(client, job_id, headers, state, timeout=2.0):
    deadline = time.time() + timeout
    resp = None
    while time.time() < deadline:
        resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        if resp.json()["state"] == state:
            return resp.json()
        time.sleep(0.02)
    pytest.fail(f"job never reached state={state}; last body={resp.json() if resp else None}")


class TestHealth:
    def test_health_does_not_require_auth(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestAuth:
    def test_missing_api_key_returns_401(self, client):
        resp = client.get("/api/v1/jobs")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_wrong_api_key_returns_401(self, client):
        resp = client.get("/api/v1/jobs", headers={"X-Api-Key": "wrong"})
        assert resp.status_code == 401


class TestCreateMovie:
    def test_rejects_non_einthusan_url(self, client, auth_headers):
        resp = client.post("/api/v1/movies", json={"url": "https://example.com/x"}, headers=auth_headers)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_url"

    def test_creates_job_and_resolves_to_awaiting_verification(self, client, auth_headers, monkeypatch):
        url = "https://einthusan.tv/movie/watch/abc123/"
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=_resolved(url)))

        resp = client.post("/api/v1/movies", json={"url": url}, headers=auth_headers)
        assert resp.status_code == 202
        job_id = resp.json()["id"]
        assert resp.json()["state"] in ("resolving", "awaiting_verification")

        body = _wait_for_state(client, job_id, auth_headers, "awaiting_verification")
        assert len(body["candidates"]) == 2
        assert body["selected_tmdb_id"] == 111

    def test_resolve_failure_surfaces_as_resolve_failed(self, client, auth_headers, monkeypatch):
        url = "https://einthusan.tv/movie/watch/abc123/"
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(side_effect=importer.ResolveError("no results")))

        resp = client.post("/api/v1/movies", json={"url": url}, headers=auth_headers)
        job_id = resp.json()["id"]

        body = _wait_for_state(client, job_id, auth_headers, "resolve_failed")
        assert body["error"]["code"] == "resolve_failed"


class TestGetJob:
    def test_returns_404_for_unknown_job(self, client, auth_headers):
        resp = client.get("/api/v1/jobs/nonexistent", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "job_not_found"


class TestPatchJob:
    def _job_awaiting_verification(self, client, auth_headers, monkeypatch):
        url = "https://einthusan.tv/movie/watch/abc123/"
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=_resolved(url)))
        resp = client.post("/api/v1/movies", json={"url": url}, headers=auth_headers)
        job_id = resp.json()["id"]
        _wait_for_state(client, job_id, auth_headers, "awaiting_verification")
        return job_id

    def test_accepts_tmdb_id_from_candidates(self, client, auth_headers, monkeypatch):
        job_id = self._job_awaiting_verification(client, auth_headers, monkeypatch)

        resp = client.patch(f"/api/v1/jobs/{job_id}", json={"tmdb_id": 222}, headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["selected_tmdb_id"] == 222

    def test_rejects_tmdb_id_not_in_candidates(self, client, auth_headers, monkeypatch):
        job_id = self._job_awaiting_verification(client, auth_headers, monkeypatch)

        resp = client.patch(f"/api/v1/jobs/{job_id}", json={"tmdb_id": 999999}, headers=auth_headers)

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "tmdb_not_in_candidates"

    def test_returns_404_for_unknown_job(self, client, auth_headers):
        resp = client.patch("/api/v1/jobs/nonexistent", json={"tmdb_id": 111}, headers=auth_headers)
        assert resp.status_code == 404


class TestStartDownload:
    def _job_awaiting_verification(self, client, auth_headers, monkeypatch):
        url = "https://einthusan.tv/movie/watch/abc123/"
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=_resolved(url)))
        resp = client.post("/api/v1/movies", json={"url": url}, headers=auth_headers)
        job_id = resp.json()["id"]
        _wait_for_state(client, job_id, auth_headers, "awaiting_verification")
        return job_id

    def test_returns_404_for_unknown_job(self, client, auth_headers):
        resp = client.post("/api/v1/jobs/nonexistent/download", headers=auth_headers)
        assert resp.status_code == 404

    def test_starts_download_and_completes(self, client, auth_headers, monkeypatch):
        job_id = self._job_awaiting_verification(client, auth_headers, monkeypatch)
        monkeypatch.setattr(importer, "add_to_radarr", MagicMock(return_value=42))
        monkeypatch.setattr(importer, "run_download_and_import", MagicMock(return_value=Path("/staging/Sabdham (2025).mp4")))

        resp = client.post(f"/api/v1/jobs/{job_id}/download", headers=auth_headers)
        assert resp.status_code == 202

        body = _wait_for_state(client, job_id, auth_headers, "done")
        assert body["result"]["radarr_movie_id"] == 42
        assert body["result"]["tmdb_id"] == 111

    def test_radarr_failure_on_add_returns_502(self, client, auth_headers, monkeypatch):
        job_id = self._job_awaiting_verification(client, auth_headers, monkeypatch)
        monkeypatch.setattr(importer, "add_to_radarr", MagicMock(side_effect=importer.RadarrUnavailableError("down")))

        resp = client.post(f"/api/v1/jobs/{job_id}/download", headers=auth_headers)

        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "radarr_unavailable"

    def test_rejects_second_download_call_while_already_downloading(self, client, auth_headers, monkeypatch):
        job_id = self._job_awaiting_verification(client, auth_headers, monkeypatch)
        monkeypatch.setattr(importer, "add_to_radarr", MagicMock(return_value=42))

        def slow_download(*a, **kw):
            time.sleep(0.2)
            return Path("/staging/x.mp4")

        monkeypatch.setattr(importer, "run_download_and_import", slow_download)

        first = client.post(f"/api/v1/jobs/{job_id}/download", headers=auth_headers)
        assert first.status_code == 202
        time.sleep(0.05)  # let the background thread flip state to "downloading"
        second = client.post(f"/api/v1/jobs/{job_id}/download", headers=auth_headers)
        assert second.status_code == 409


class TestListJobs:
    def test_lists_created_jobs(self, client, auth_headers, monkeypatch):
        url = "https://einthusan.tv/movie/watch/abc123/"
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=_resolved(url)))
        client.post("/api/v1/movies", json={"url": url}, headers=auth_headers)

        resp = client.get("/api/v1/jobs", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestDeleteJob:
    def test_returns_404_for_unknown_job(self, client, auth_headers):
        resp = client.delete("/api/v1/jobs/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    def test_deletes_job_awaiting_verification(self, client, auth_headers, monkeypatch):
        url = "https://einthusan.tv/movie/watch/abc123/"
        monkeypatch.setattr(importer, "resolve_movie", MagicMock(return_value=_resolved(url)))
        resp = client.post("/api/v1/movies", json={"url": url}, headers=auth_headers)
        job_id = resp.json()["id"]
        _wait_for_state(client, job_id, auth_headers, "awaiting_verification")

        del_resp = client.delete(f"/api/v1/jobs/{job_id}", headers=auth_headers)

        assert del_resp.status_code == 204
        assert client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers).status_code == 404
```

   Add `from pathlib import Path` to this test file's imports too.

3. Run to verify fail: `.venv/bin/pytest tests/test_api.py -v` — modules don't exist.

4. Implement `api/auth.py`:

```python
"""Static API-key authentication dependency."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.api_key
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "Missing or invalid X-Api-Key header"}},
        )
```

5. Implement `api/models.py`:

```python
"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateMovieRequest(BaseModel):
    url: str = Field(..., description="Einthusan movie page URL")


class TmdbCandidateOut(BaseModel):
    tmdb_id: int
    title: str
    year: int
    tmdb_url: str
    poster_url: str | None = None


class ProgressOut(BaseModel):
    downloaded_bytes: int
    total_bytes: int
    percent: float
    speed_bps: float
    eta_seconds: float | None = None


class ErrorOut(BaseModel):
    code: str
    message: str


class ResultOut(BaseModel):
    file: str
    radarr_movie_id: int
    tmdb_id: int


class JobOut(BaseModel):
    id: str
    state: str
    einthusan_url: str
    candidates: list[TmdbCandidateOut] = []
    selected_tmdb_id: int | None = None
    progress: ProgressOut | None = None
    result: ResultOut | None = None
    error: ErrorOut | None = None


class PatchJobRequest(BaseModel):
    tmdb_id: int


class HealthOut(BaseModel):
    status: str = "ok"
```

6. Implement `api/routes.py`:

```python
"""HTTP routes for the einthusan-downloader API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

import importer
from api import worker
from api.auth import require_api_key
from api.models import (
    CreateMovieRequest,
    ErrorOut,
    HealthOut,
    JobOut,
    PatchJobRequest,
    ProgressOut,
    ResultOut,
    TmdbCandidateOut,
)

router = APIRouter()


def _job_to_out(job) -> JobOut:
    return JobOut(
        id=job.id,
        state=job.state,
        einthusan_url=job.einthusan_url,
        candidates=[TmdbCandidateOut(**vars(c)) for c in job.candidates],
        selected_tmdb_id=job.selected_tmdb_id,
        progress=ProgressOut(**vars(job.progress)) if job.state in ("downloading", "importing") else None,
        result=ResultOut(**job.result) if job.result else None,
        error=ErrorOut(**job.error) if job.error else None,
    )


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut()


@router.post("/movies", response_model=JobOut, status_code=202, dependencies=[Depends(require_api_key)])
def create_movie(body: CreateMovieRequest, request: Request) -> JobOut:
    if not importer.EINTHUSAN_URL_RE.match(body.url):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "invalid_url", "message": "Not a valid Einthusan movie URL"}},
        )
    store = request.app.state.job_store
    job = store.create(body.url)
    worker.submit_resolve(store, request.app.state.settings.core_config, job)
    return _job_to_out(job)


@router.get("/jobs", response_model=list[JobOut], dependencies=[Depends(require_api_key)])
def list_jobs(request: Request) -> list[JobOut]:
    return [_job_to_out(j) for j in request.app.state.job_store.list()]


@router.get("/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_api_key)])
def get_job(job_id: str, request: Request) -> JobOut:
    job = request.app.state.job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "job_not_found", "message": "No such job"}})
    return _job_to_out(job)


@router.patch("/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_api_key)])
def patch_job(job_id: str, body: PatchJobRequest, request: Request) -> JobOut:
    store = request.app.state.job_store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "job_not_found", "message": "No such job"}})
    if job.state != "awaiting_verification":
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "invalid_state", "message": f"Job is in state '{job.state}', not 'awaiting_verification'"}},
        )
    if body.tmdb_id not in {c.tmdb_id for c in job.candidates}:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "tmdb_not_in_candidates", "message": "tmdb_id must be one of the job's candidates"}},
        )
    store.update(job_id, selected_tmdb_id=body.tmdb_id)
    return _job_to_out(store.get(job_id))


@router.post("/jobs/{job_id}/download", response_model=JobOut, status_code=202, dependencies=[Depends(require_api_key)])
def start_download(job_id: str, request: Request) -> JobOut:
    store = request.app.state.job_store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "job_not_found", "message": "No such job"}})
    if job.state != "awaiting_verification":
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "invalid_state", "message": f"Job is in state '{job.state}', not 'awaiting_verification'"}},
        )
    candidate = next(c for c in job.candidates if c.tmdb_id == job.selected_tmdb_id)
    try:
        radarr_movie_id = importer.add_to_radarr(request.app.state.settings.core_config, candidate, monitored=False)
    except importer.ImporterError as exc:
        raise HTTPException(status_code=502, detail={"error": {"code": exc.code, "message": str(exc)}})
    store.update(job_id, radarr_movie_id=radarr_movie_id)
    worker.submit_download(store, request.app.state.settings.core_config, store.get(job_id))
    return _job_to_out(store.get(job_id))


@router.delete("/jobs/{job_id}", status_code=204, dependencies=[Depends(require_api_key)])
def delete_job(job_id: str, request: Request) -> None:
    store = request.app.state.job_store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "job_not_found", "message": "No such job"}})
    if job.state in ("downloading", "importing"):
        store.update(job_id, cancelled=True)
    if job.radarr_movie_id and job.state != "done":
        try:
            importer.remove_from_radarr(request.app.state.settings.core_config, job.radarr_movie_id, delete_files=True)
        except importer.ImporterError:
            pass
    store.delete(job_id)
```

   Note on `test_rejects_second_download_call_while_already_downloading`: `_run_download` is submitted to the 1-worker `download_pool`, but the state guard here is checked in the route handler (`job.state != "awaiting_verification"`) *before* resubmitting — `_run_download` sets `state="downloading"` as its very first action, so a second `POST .../download` arriving after the small `time.sleep(0.05)` in the test correctly observes `state == "downloading"` and 409s.

7. Implement `api/main.py`:

```python
"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from api.jobs import JobStore
from api.routes import router
from api.settings import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="einthusan-downloader API", version="1")
    app.state.settings = settings or load_settings()
    app.state.job_store = JobStore()
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": "http_error", "message": str(exc.detail)}})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": str(exc)}})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": {"code": "internal", "message": str(exc)}})

    return app
```

8. Implement `api/__main__.py`:

```python
"""Entry point: `python -m api`."""

import os

import uvicorn

from api.main import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

9. Run to verify pass: `.venv/bin/pytest tests/test_api.py -v` — all green. (If any timing-sensitive test flakes on `_wait_for_state`, verify `resolve_pool`/`download_pool` are module-level singletons in `api/worker.py` imported correctly, not re-created per request.)

10. Run full suite: `.venv/bin/pytest tests/ -v` — green.

11. Commit: `feat: add FastAPI app (api/auth, models, routes, main, __main__)`

---

## Task 9: `api_client.py` — HTTP client for Streamlit

**Files:**
- `api_client.py` (new)
- `tests/test_api_client.py` (new)

**Interfaces:**
```python
class EinthusanApiError(Exception):
    def __init__(self, code: str, message: str, status: int): ...

@dataclass(frozen=True)
class TmdbCandidateView: ...

@dataclass(frozen=True)
class JobView:
    @classmethod
    def from_json(cls, data: dict) -> "JobView": ...

class EinthusanApiClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0): ...
    def health(self) -> dict: ...
    def create_job(self, url: str) -> JobView: ...
    def get_job(self, job_id: str) -> JobView: ...
    def patch_job(self, job_id: str, tmdb_id: int) -> JobView: ...
    def start_download(self, job_id: str) -> JobView: ...
    def delete_job(self, job_id: str) -> None: ...
    def list_jobs(self) -> list[JobView]: ...
```

### Steps

1. Write failing test `tests/test_api_client.py`, mocking HTTP with `responses`:

```python
import sys
from pathlib import Path

import pytest
import responses

sys.path.insert(0, str(Path(__file__).parent.parent))

from api_client import EinthusanApiClient, EinthusanApiError, JobView

BASE = "http://localhost:8000"
API_KEY = "test-key"

SAMPLE_JOB_JSON = {
    "id": "job-1",
    "state": "awaiting_verification",
    "einthusan_url": "https://einthusan.tv/movie/watch/abc123/",
    "candidates": [
        {"tmdb_id": 111, "title": "Sabdham", "year": 2025, "tmdb_url": "https://tmdb/111", "poster_url": None}
    ],
    "selected_tmdb_id": 111,
    "progress": None,
    "result": None,
    "error": None,
}


@pytest.fixture
def client():
    return EinthusanApiClient(BASE, API_KEY)


class TestHealth:
    @responses.activate
    def test_health_returns_status(self, client):
        responses.add(responses.GET, f"{BASE}/api/v1/health", json={"status": "ok"}, status=200)

        result = client.health()

        assert result == {"status": "ok"}


class TestCreateJob:
    @responses.activate
    def test_sends_api_key_header_and_returns_job_view(self, client):
        responses.add(responses.POST, f"{BASE}/api/v1/movies", json=SAMPLE_JOB_JSON, status=202)

        job = client.create_job("https://einthusan.tv/movie/watch/abc123/")

        assert isinstance(job, JobView)
        assert job.id == "job-1"
        assert job.candidates[0].tmdb_id == 111
        sent_headers = responses.calls[0].request.headers
        assert sent_headers["X-Api-Key"] == API_KEY

    @responses.activate
    def test_raises_api_error_with_code_on_422(self, client):
        responses.add(
            responses.POST, f"{BASE}/api/v1/movies",
            json={"error": {"code": "invalid_url", "message": "Not a valid Einthusan movie URL"}},
            status=422,
        )

        with pytest.raises(EinthusanApiError) as exc_info:
            client.create_job("not-a-url")

        assert exc_info.value.code == "invalid_url"
        assert exc_info.value.status == 422


class TestGetJob:
    @responses.activate
    def test_returns_job_view(self, client):
        responses.add(responses.GET, f"{BASE}/api/v1/jobs/job-1", json=SAMPLE_JOB_JSON, status=200)

        job = client.get_job("job-1")

        assert job.state == "awaiting_verification"

    @responses.activate
    def test_raises_on_404(self, client):
        responses.add(
            responses.GET, f"{BASE}/api/v1/jobs/nonexistent",
            json={"error": {"code": "job_not_found", "message": "No such job"}}, status=404,
        )

        with pytest.raises(EinthusanApiError) as exc_info:
            client.get_job("nonexistent")

        assert exc_info.value.code == "job_not_found"
        assert exc_info.value.status == 404


class TestPatchJob:
    @responses.activate
    def test_sends_tmdb_id_and_returns_updated_job(self, client):
        updated = {**SAMPLE_JOB_JSON, "selected_tmdb_id": 222}
        responses.add(responses.PATCH, f"{BASE}/api/v1/jobs/job-1", json=updated, status=200)

        job = client.patch_job("job-1", 222)

        assert job.selected_tmdb_id == 222
        assert responses.calls[0].request.body == b'{"tmdb_id": 222}'


class TestStartDownload:
    @responses.activate
    def test_returns_job_view(self, client):
        downloading = {**SAMPLE_JOB_JSON, "state": "downloading"}
        responses.add(responses.POST, f"{BASE}/api/v1/jobs/job-1/download", json=downloading, status=202)

        job = client.start_download("job-1")

        assert job.state == "downloading"


class TestDeleteJob:
    @responses.activate
    def test_sends_delete_request(self, client):
        responses.add(responses.DELETE, f"{BASE}/api/v1/jobs/job-1", status=204)

        client.delete_job("job-1")

        assert responses.calls[0].request.method == "DELETE"


class TestListJobs:
    @responses.activate
    def test_returns_list_of_job_views(self, client):
        responses.add(responses.GET, f"{BASE}/api/v1/jobs", json=[SAMPLE_JOB_JSON], status=200)

        jobs = client.list_jobs()

        assert len(jobs) == 1
        assert jobs[0].id == "job-1"
```

2. Run to verify fail: `.venv/bin/pytest tests/test_api_client.py -v` — module doesn't exist.

3. Implement `api_client.py`:

```python
"""HTTP client for the einthusan-downloader API, used by app.py (Streamlit)."""

from __future__ import annotations

from dataclasses import dataclass

import requests


class EinthusanApiError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class TmdbCandidateView:
    tmdb_id: int
    title: str
    year: int
    tmdb_url: str
    poster_url: str | None


@dataclass(frozen=True)
class JobView:
    id: str
    state: str
    einthusan_url: str
    candidates: list[TmdbCandidateView]
    selected_tmdb_id: int | None
    progress: dict | None
    result: dict | None
    error: dict | None

    @classmethod
    def from_json(cls, data: dict) -> "JobView":
        return cls(
            id=data["id"],
            state=data["state"],
            einthusan_url=data["einthusan_url"],
            candidates=[TmdbCandidateView(**c) for c in data.get("candidates", [])],
            selected_tmdb_id=data.get("selected_tmdb_id"),
            progress=data.get("progress"),
            result=data.get("result"),
            error=data.get("error"),
        )


class EinthusanApiClient:
    """Thin wrapper over the HTTP API. Raises EinthusanApiError on any non-2xx
    response, with `.code` taken from the API's {"error": {"code", "message"}}
    envelope."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"X-Api-Key": api_key})

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        if not resp.ok:
            try:
                body = resp.json()
                err = body.get("error", {})
                code = err.get("code", "unknown")
                message = err.get("message", resp.text)
            except ValueError:
                code, message = "unknown", resp.text
            raise EinthusanApiError(code, message, resp.status_code)
        return resp.json() if resp.content else {}

    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    def create_job(self, url: str) -> JobView:
        return JobView.from_json(self._request("POST", "/api/v1/movies", json={"url": url}))

    def get_job(self, job_id: str) -> JobView:
        return JobView.from_json(self._request("GET", f"/api/v1/jobs/{job_id}"))

    def patch_job(self, job_id: str, tmdb_id: int) -> JobView:
        return JobView.from_json(self._request("PATCH", f"/api/v1/jobs/{job_id}", json={"tmdb_id": tmdb_id}))

    def start_download(self, job_id: str) -> JobView:
        return JobView.from_json(self._request("POST", f"/api/v1/jobs/{job_id}/download"))

    def delete_job(self, job_id: str) -> None:
        self._request("DELETE", f"/api/v1/jobs/{job_id}")

    def list_jobs(self) -> list[JobView]:
        return [JobView.from_json(j) for j in self._request("GET", "/api/v1/jobs")]
```

4. Run to verify pass: `.venv/bin/pytest tests/test_api_client.py -v` — all green.

5. Run full suite: `.venv/bin/pytest tests/ -v` — green.

6. Commit: `feat: add api_client.py`

---

## Task 10: `app.py` — rewrite as a thin API client

**Why:** Per the explicit user constraint, Streamlit must stop duplicating orchestration logic and instead drive the same API a Flutter client would. `_background_import`, `_run_preview`, `QueueLogHandler`, `ListLogHandler`, and the daemon-thread/queue machinery are all deleted — polling `GET /jobs/{id}` replaces them.

**Files:**
- `app.py` (fully rewritten)

### Steps

1. There's no headless test harness for Streamlit pages in this repo, and the design spec doesn't call for one — `app.py`'s only extracted logic (`api_client.py`) is already covered by Task 9's tests. Verification here is an import/syntax smoke check plus the manual test plan in Task 12.

2. Replace `app.py` in full:

```python
"""
Streamlit web UI for einthusan-dl.

Thin HTTP client over the einthusan-downloader API (see api/). All
orchestration (login, scraping, Radarr, download, import) lives server-side
in importer.py / api/worker.py; this file only renders job state and lets the
user verify the TMDB match before triggering the download.

Run locally (API must already be running — see api/__main__.py):
    .venv/bin/streamlit run app.py
"""

import os
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from api_client import EinthusanApiClient, EinthusanApiError

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

st.set_page_config(
    page_title="Einthusan Downloader",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

POLL_SECONDS = 1.5


def _init_state():
    st.session_state.setdefault("job_id", None)
    st.session_state.setdefault("step", "input")
    st.session_state.setdefault("error", "")
    st.session_state.setdefault("api_base", os.environ.get("EINTHUSAN_API_BASE", "http://localhost:8000"))
    st.session_state.setdefault("api_key", os.environ.get("EINTHUSAN_API_KEY", ""))


def _client() -> EinthusanApiClient:
    return EinthusanApiClient(st.session_state["api_base"], st.session_state["api_key"])


def _sidebar():
    st.sidebar.header("Connection")
    st.session_state["api_base"] = st.sidebar.text_input("API base URL", value=st.session_state["api_base"])
    st.session_state["api_key"] = st.sidebar.text_input("API key", value=st.session_state["api_key"], type="password")


def _reset():
    st.session_state["job_id"] = None
    st.session_state["step"] = "input"
    st.session_state["error"] = ""


def _fail(exc: EinthusanApiError):
    st.session_state["error"] = f"{exc.code}: {exc.message}"
    st.session_state["step"] = "error"


def _page_input():
    st.title("🎬 Einthusan Downloader")
    st.caption("Download Tamil movies from Einthusan.tv and import them into Radarr / Jellyfin.")

    with st.form("url_form"):
        url = st.text_input("Einthusan movie URL", placeholder="https://einthusan.tv/movie/watch/...")
        submitted = st.form_submit_button("Fetch details")

    if submitted and url:
        try:
            job = _client().create_job(url)
        except EinthusanApiError as exc:
            _fail(exc)
        else:
            st.session_state["job_id"] = job.id
            st.session_state["step"] = "resolving"
        st.rerun()


def _page_resolving():
    st.title("🔍 Looking up movie details …")
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return

    if job.state == "resolve_failed":
        message = job.error.get("message", "Resolve failed") if job.error else "Resolve failed"
        st.session_state["error"] = message
        st.session_state["step"] = "error"
        st.rerun()
        return

    if job.state == "awaiting_verification":
        st.session_state["step"] = "preview"
        st.rerun()
        return

    with st.spinner("Fetching page and searching TMDB …"):
        time.sleep(POLL_SECONDS)
    st.rerun()


def _page_preview():
    job = _client().get_job(st.session_state["job_id"])
    st.title("✅ Confirm the match")

    options = {f"{c.title} ({c.year}) — tmdb:{c.tmdb_id}": c.tmdb_id for c in job.candidates}
    labels = list(options.keys())
    default_label = next((label for label, tid in options.items() if tid == job.selected_tmdb_id), labels[0])
    choice = st.radio("TMDB match", labels, index=labels.index(default_label))
    chosen_tmdb_id = options[choice]

    for c in job.candidates:
        if c.tmdb_id == chosen_tmdb_id:
            if c.poster_url:
                st.image(c.poster_url, width=200)
            st.markdown(f"[View on TMDB]({c.tmdb_url})")

    col1, col2 = st.columns(2)
    if col1.button("Confirm and download", type="primary"):
        try:
            if chosen_tmdb_id != job.selected_tmdb_id:
                _client().patch_job(job.id, chosen_tmdb_id)
            _client().start_download(job.id)
        except EinthusanApiError as exc:
            _fail(exc)
        else:
            st.session_state["step"] = "running"
        st.rerun()

    if col2.button("Cancel"):
        try:
            _client().delete_job(job.id)
        except EinthusanApiError:
            pass
        _reset()
        st.rerun()


def _page_running():
    st.title("⏳ Downloading and importing …")
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return

    if job.state == "error":
        message = job.error.get("message", "Import failed") if job.error else "Import failed"
        st.session_state["error"] = message
        st.session_state["step"] = "error"
        st.rerun()
        return

    if job.state == "done":
        st.session_state["step"] = "done"
        st.rerun()
        return

    if job.progress and job.progress.get("total_bytes"):
        st.progress(min(job.progress["percent"] / 100, 1.0))
        st.caption(
            f"{job.progress['downloaded_bytes'] / 1e6:.1f} MB / "
            f"{job.progress['total_bytes'] / 1e6:.1f} MB — "
            f"{job.progress['speed_bps'] / 1e6:.2f} MB/s"
        )
    else:
        st.spinner("Working …")

    time.sleep(POLL_SECONDS)
    st.rerun()


def _page_done():
    st.title("🎉 Done!")
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return

    if job.result:
        st.success(f"Imported: {job.result['file']}")
        st.write(f"Radarr movie ID: {job.result['radarr_movie_id']}")
    if st.button("Import another movie"):
        _reset()
        st.rerun()


def _page_error():
    st.title("❌ Something went wrong")
    st.error(st.session_state.get("error", "Unknown error"))
    if st.button("Start over"):
        _reset()
        st.rerun()


def main():
    _init_state()
    _sidebar()
    step = st.session_state["step"]
    pages = {
        "input": _page_input,
        "resolving": _page_resolving,
        "preview": _page_preview,
        "running": _page_running,
        "done": _page_done,
    }
    pages.get(step, _page_error)()


if __name__ == "__main__":
    main()
```

3. Verify it at least imports cleanly (catches syntax errors and missing-import mistakes; Streamlit scripts can't run headless as a normal `pytest` target):
```bash
.venv/bin/python -c "import ast; ast.parse(open('app.py').read())"
.venv/bin/python -c "import api_client, importer"  # confirms no import cycle between app.py's dependencies
```

4. Run full suite: `.venv/bin/pytest tests/ -v` — green (app.py has no direct test file; its only extracted logic, `api_client.py`, is already covered by Task 9).

5. Commit: `refactor: rewrite app.py as a thin api_client consumer`

---

## Task 11: Docker / Compose / local dev process runner

**Why:** Two processes now need to run — `api/` (port 8000) and `app.py` via Streamlit (port 8501, unchanged) — either as two Docker Compose services sharing one image, or locally via `honcho`.

**Files:**
- `Dockerfile` (modify: switch `ENTRYPOINT` to `CMD` so `docker-compose.yaml`'s per-service `command:` can select which process runs; add fastapi/uvicorn to the builder's dependency install list)
- `docker-compose.yaml` (modify: split into `einthusan-api` + `einthusan-ui` services)
- `Procfile` (new, for local dev via `honcho start`)
- `.env.example` (new — referenced by README but absent from the repo; create it since Task 12 depends on it existing)

### Steps

1. Edit `Dockerfile`:
   - Add `fastapi`/`uvicorn` to the builder's `uv pip install` line (currently line 19-22):

```dockerfile
RUN uv venv /app/.venv && \
    uv pip install \
        --python /app/.venv/bin/python3 \
        --no-cache \
        requests beautifulsoup4 tqdm python-dotenv lxml "streamlit>=1.40.0" "playwright>=1.44.0" \
        "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0"
```

   - Copy the new application files (currently line 56: `COPY einthusan_dl.py app.py ./`):

```dockerfile
COPY einthusan_dl.py app.py importer.py api_client.py ./
COPY api ./api
```

   - Add an `EXPOSE 8000` alongside the existing `EXPOSE 8501` (line 62):

```dockerfile
EXPOSE 8501 8000
```

   - Replace the fixed `ENTRYPOINT` (line 70) with a `CMD`, so `docker-compose.yaml` can override it per service without needing a wrapper script:

```dockerfile
CMD ["/app/.venv/bin/streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

   - The existing `HEALTHCHECK` (targeting port 8501) stays as-is for the default/UI command; the API service in compose gets its own `healthcheck:` block instead (see step 2).

2. Rewrite `docker-compose.yaml`:

```yaml
services:
  einthusan-api:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: einthusan-api
    restart: unless-stopped
    user: "1006:100"   # arr-user:users — files land with correct ownership, no chown needed
    command: ["/app/.venv/bin/python3", "-m", "api"]
    ports:
      - "8503:8000"           # Flutter/other HTTP clients hit http://<host>:8503
    volumes:
      - ./.env:/app/.env:ro  # credentials (read-only)
      - /volume2/arr-data/media/manual_imports:/data/media/manual_imports
    environment:
      - STAGING_DIR_HOST=/data/media/manual_imports
      - API_PORT=8000
      # host.docker.internal resolves to the host machine, so Radarr at
      # localhost:7878 is reachable from inside the container.
      # Override in .env if Radarr is on a different machine.
      - RADARR_URL=http://host.docker.internal:7878
    extra_hosts:
      - "host.docker.internal:host-gateway"
    healthcheck:
      test: ["CMD", "/app/.venv/bin/python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')"]
      interval: 30s
      timeout: 10s
      start_period: 15s
      retries: 3

  einthusan-ui:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: einthusan-ui
    restart: unless-stopped
    user: "1006:100"
    depends_on:
      - einthusan-api
    ports:
      - "8502:8501"          # access at http://<your-server-ip>:8502
    environment:
      - EINTHUSAN_API_BASE=http://einthusan-api:8000
      - EINTHUSAN_API_KEY=${EINTHUSAN_API_KEY}
```

   (The UI service no longer mounts `.env` or the staging volume — it holds no Einthusan/Radarr credentials, only the API base URL + key, per the Global Constraints.)

3. Create `Procfile` for local two-process dev (`honcho start`, already added to `requirements.txt` in Task 8):

```
api: .venv/bin/python -m api
ui: .venv/bin/streamlit run app.py
```

4. Create `.env.example` (did not previously exist in the repo despite being referenced by README):

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
API_PORT=8000

# UI service (app.py) — only needed when running app.py against a remote API
EINTHUSAN_API_BASE=http://localhost:8000
```

5. Verify the Docker build still succeeds (this is the closest thing to a test for this task): `docker build -t einthusan-downloader-test .` — must complete without error. If Docker isn't available in the execution environment, skip this verification and note it explicitly rather than silently passing.

6. Run full suite once more (unaffected by this task, but confirms nothing broke): `.venv/bin/pytest tests/ -v` — green.

7. Commit: `chore: split Docker Compose into einthusan-api + einthusan-ui services`

---

## Task 12: Documentation — `CLAUDE.md` and `README.md`

**Files:**
- `CLAUDE.md` (update Architecture section)
- `README.md` (update Quick start, Configuration, Docker details)

### Steps

1. Update `CLAUDE.md`'s Architecture section to describe the new module layout. Replace the current "Two entry points share the same core library" paragraph and the `app.py`/`einthusan_dl.py` subsections with:

```markdown
### Three layers share `einthusan_dl.py`

- **`einthusan_dl.py`** — `EinthusanClient` (auth + scraping) and `RadarrClient` (Radarr v3 API wrapper), plus a `main()`/argparse CLI for headless use. Unchanged low-level behavior; see below for exact mechanisms.
- **`importer.py`** — framework-free orchestration: `resolve_movie` (login + scrape + TMDB lookup via Radarr), `add_to_radarr` (add-or-reuse + tag, unmonitored by default), `run_download_and_import` (flip monitored, download with one retry via a freshly re-resolved session, manual import with `DownloadedMoviesScan` fallback). Every failure raises an `ImporterError` subclass carrying a stable `.code` string consumed by the API.
- **`api/`** — FastAPI + uvicorn service (`python -m api`, port 8000 by default) that is the *only* thing holding Einthusan/Radarr credentials at runtime. Exposes a job-based workflow (`POST /api/v1/movies` → `awaiting_verification` → `PATCH` to correct the TMDB match → `POST /api/v1/jobs/{id}/download` → poll `GET /api/v1/jobs/{id}`) backed by an in-memory `JobStore` and two `ThreadPoolExecutor`s (`resolve_pool`, 2 workers; `download_pool`, 1 worker — serial downloads). Auth is a static `X-Api-Key` header (`EINTHUSAN_API_KEY`). See `docs/superpowers/specs/2026-09-07-einthusan-http-api-design.md` for the full endpoint/error-code reference.
- **`app.py`** — Streamlit UI, now a thin client of the API via `api_client.py::EinthusanApiClient`. Holds no Einthusan/Radarr credentials — only `EINTHUSAN_API_BASE` + `EINTHUSAN_API_KEY`. State machine: `input → resolving → preview → running → done | error`, driven by polling `GET /api/v1/jobs/{id}` every ~1.5s instead of the old daemon-thread + `queue.Queue` bridge.
```

   Update the "Run the Streamlit UI locally" command block to note the API dependency:

```markdown
**Run the API + Streamlit UI locally (two processes):**
```bash
.venv/bin/honcho start   # runs both `api` and `ui` from Procfile
# or individually:
.venv/bin/python -m api               # API on :8000
.venv/bin/streamlit run app.py        # UI on :8501, set EINTHUSAN_API_BASE to point at it
```
```

   Update the Docker section (`docker compose up --build -d`) to note the two services:

```markdown
**Docker:**
```bash
docker compose up --build -d
# UI at http://<host>:8502, API at http://<host>:8503
```
```

2. Update `README.md`:

   - "How it works" — steps 2 and 5 should say "The API" instead of "the app" now that the work happens server-side.
   - "Quick start (Docker)" — update step 4 to mention both ports:

```markdown
### 4. Open the UI
http://<your-server-ip>:8502

The Flutter/HTTP API is available separately at http://<your-server-ip>:8503 (requires the `X-Api-Key` header — see Configuration).
```

   - "Configuration" section — replace the `.env` example block with the same content as the new `.env.example` from Task 11, and add:

```markdown
The Streamlit UI (`einthusan-ui` service) no longer needs Einthusan or Radarr
credentials directly — it only needs `EINTHUSAN_API_BASE` (defaults to
`http://einthusan-api:8000` inside Docker Compose) and `EINTHUSAN_API_KEY`.
All actual credentials live only in the `einthusan-api` service's `.env`.
```

   - "Docker details" section — update the port table:

```markdown
```
host port 8502  →  einthusan-ui container port 8501 (Streamlit)
host port 8503  →  einthusan-api container port 8000 (HTTP API)
```
```

3. There is no automated test for documentation content; verify by re-reading both files end-to-end after editing to confirm no leftover references to the deleted `STAGING_DIR_RADARR` env var (already noted as dead in the current CLAUDE.md) or to the old single-service compose file remain.

4. Run full suite one last time: `.venv/bin/pytest tests/ -v` — green.

5. Commit: `docs: update CLAUDE.md and README.md for the split API/UI architecture`

---

## Self-Review Checklist

- [x] **Spec coverage:** all 7 endpoints (health, POST movies, GET job, PATCH job, POST download, DELETE job, GET jobs), all 7 job states, the error-code table, the `EINTHUSAN_API_KEY`/`EINTHUSAN_API_BASE`/`API_PORT` env vars, and the two-Compose-service Docker split from the spec are each covered by a task above.
- [x] **Placeholder scan:** no task step contains "TBD", "similar to Task N", or unfilled code — every function body above is complete, runnable Python (or complete YAML/Dockerfile/env-file content).
- [x] **Signature consistency:** `TmdbCandidate`/`ResolvedMovie`/`ImporterError` hierarchy defined once in Task 2 and referenced identically (same field names/types) in Tasks 3, 4, 6, 7, 8, 9; `RadarrClient.add_movie(..., monitored: bool = True)` signature from Task 1 matches its call sites in Task 3's `add_to_radarr`; `Progress`/`Job`/`JobStore` from Task 6 match their usage in Task 7's `worker.py` and Task 8's `routes.py`.
- [x] **Open questions from the spec resolved:** poster_url sourced from Radarr lookup's `images[].remoteUrl|url` (Task 2); no `queued` state added (worker.py's 1-worker pool naturally serializes; jobs simply stay `downloading` behind it); no CDN-resolution alias methods needed since `get_movie_info` already returns a resolved `video_url` (Task 2/4 retry re-calls `resolve_movie` wholesale instead).
- [x] **Test-first ordering:** every task's steps write a failing test before the implementation, per the mandatory TDD workflow.

---

## Execution Handoff

Choose how to execute this plan:

1. **Subagent-Driven (recommended)** — `superpowers:subagent-driven-development` dispatches each task to a fresh subagent with clean context, verifies its tests pass, and commits before moving to the next task. Best for a plan this size (12 tasks) since it avoids context buildup across tasks.
2. **Inline Execution** — `superpowers:executing-plans` runs through the tasks in this same session, one at a time.
