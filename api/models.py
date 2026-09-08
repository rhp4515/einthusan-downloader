"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from api.jobs import JobState


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
    state: JobState
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
