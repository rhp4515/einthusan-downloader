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
