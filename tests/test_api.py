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
