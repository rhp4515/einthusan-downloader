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
    DownloadError,
    ImportFailedError,
    ResolveError,
    ResolvedMovie,
    TmdbCandidate,
    resolve_movie,
    run_download_and_import,
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
        "https://einthusan.tv/premium/movie/watch/4QmG/?lang=tamil",
        "https://einthusan.tv/movie/watch/abc123/?lang=hindi",
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


SABDHAM_CANDIDATE = TmdbCandidate(
    tmdb_id=111,
    title="Sabdham",
    year=2025,
    tmdb_url="https://www.themoviedb.org/movie/111",
    poster_url=None,
)


class TestAddToRadarr:
    def test_adds_new_movie_unmonitored_by_default(self, monkeypatch):
        from importer import add_to_radarr
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
        from importer import add_to_radarr
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.return_value = 5
        fake_radarr.get_existing_movie.return_value = None
        fake_radarr.add_movie.return_value = {"id": 42}
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        add_to_radarr(CFG, SABDHAM_CANDIDATE, monitored=True)

        _, kwargs = fake_radarr.add_movie.call_args
        assert kwargs["monitored"] is True

    def test_reuses_existing_movie_and_tags_it(self, monkeypatch):
        from importer import add_to_radarr
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.return_value = 5
        fake_radarr.get_existing_movie.return_value = {"id": 99, "tags": []}
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        movie_id = add_to_radarr(CFG, SABDHAM_CANDIDATE)

        assert movie_id == 99
        fake_radarr.add_movie.assert_not_called()
        fake_radarr.update_movie_tags.assert_called_once_with({"id": 99, "tags": []}, [5])

    def test_raises_radarr_unavailable_on_failure(self, monkeypatch):
        from importer import add_to_radarr
        fake_radarr = MagicMock()
        fake_radarr.get_or_create_tag.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        with pytest.raises(importer.RadarrUnavailableError):
            add_to_radarr(CFG, SABDHAM_CANDIDATE)


class TestRemoveFromRadarr:
    def test_deletes_movie_without_files_by_default(self, monkeypatch):
        from importer import remove_from_radarr
        fake_radarr = MagicMock()
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        remove_from_radarr(CFG, 42)

        fake_radarr.delete_movie.assert_called_once_with(42, delete_files=False)

    def test_deletes_movie_with_files_when_requested(self, monkeypatch):
        from importer import remove_from_radarr
        fake_radarr = MagicMock()
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        remove_from_radarr(CFG, 42, delete_files=True)

        fake_radarr.delete_movie.assert_called_once_with(42, delete_files=True)

    def test_raises_radarr_unavailable_on_failure(self, monkeypatch):
        from importer import remove_from_radarr
        fake_radarr = MagicMock()
        fake_radarr.delete_movie.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(importer, "RadarrClient", MagicMock(return_value=fake_radarr))

        with pytest.raises(importer.RadarrUnavailableError):
            remove_from_radarr(CFG, 42)


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

    def test_falls_back_to_downloaded_movies_scan_when_approve_fails(self, monkeypatch):
        fake_radarr = self._fake_radarr(monkeypatch)
        fake_radarr.manual_import_approve.side_effect = RuntimeError("Radarr 500")
        self._fake_einthusan_new(monkeypatch)

        run_download_and_import(CFG, resolved=_resolved_movie(), candidate=SABDHAM_CANDIDATE, radarr_movie_id=42)

        expected_path = str(Path(CFG["staging_host"]) / "Sabdham (2025).mp4")
        fake_radarr.downloaded_movies_scan.assert_called_once_with(expected_path)
