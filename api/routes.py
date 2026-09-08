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
