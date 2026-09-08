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
    r"^https?://(?:www\.)?einthusan\.tv/(?:premium/)?movie/watch/[^/?#]+/?(?:\?[^#]*)?(?:#.*)?$",
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
