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

    def test_progress_callback_first_sample_reports_no_speed_or_eta_then_second_sample_computes_real_values(self, monkeypatch):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        cb = worker._make_progress_callback(store, job_id)

        # Increment on every call to time.time() (both worker.py's own calls
        # and the ones JobStore.update makes internally for updated_at,
        # since "time" is a shared module singleton) so the second sample
        # is guaranteed to look >= _PROGRESS_SAMPLE_SECONDS later than the
        # first, without relying on wall-clock sleeps.
        clock = {"now": 1000.0}

        def fake_time():
            clock["now"] += 0.3
            return clock["now"]

        monkeypatch.setattr(worker.time, "time", fake_time)

        cb(10, 100)
        first = store.get(job_id)
        assert first.progress.speed_bps == 0.0
        assert first.progress.eta_seconds is None

        cb(60, 100)
        second = store.get(job_id)
        assert second.progress.speed_bps > 0
        assert second.progress.eta_seconds is not None

    def test_progress_callback_raises_when_job_cancelled(self):
        store = JobStore()
        job_id = self._job_ready_for_download(store)
        store.update(job_id, cancelled=True)
        cb = worker._make_progress_callback(store, job_id)

        with pytest.raises(importer.DownloadCancelled):
            cb(10, 100)
