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
        try:
            resp = self._session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise EinthusanApiError("unreachable", str(exc), 0) from exc
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
