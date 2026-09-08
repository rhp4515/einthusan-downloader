import sys
from pathlib import Path

import pytest
import requests
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


class TestConnectionFailure:
    @responses.activate
    def test_health_raises_unreachable_on_connection_error(self, client):
        responses.add(responses.GET, f"{BASE}/api/v1/health", body=requests.exceptions.ConnectionError())

        with pytest.raises(EinthusanApiError) as exc_info:
            client.health()

        assert exc_info.value.code == "unreachable"
        assert exc_info.value.status == 0


class TestListJobs:
    @responses.activate
    def test_returns_list_of_job_views(self, client):
        responses.add(responses.GET, f"{BASE}/api/v1/jobs", json=[SAMPLE_JOB_JSON], status=200)

        jobs = client.list_jobs()

        assert len(jobs) == 1
        assert jobs[0].id == "job-1"
