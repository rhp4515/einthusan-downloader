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
