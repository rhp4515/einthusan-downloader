"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from api.jobs import JobStore
from api.routes import router
from api.settings import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="einthusan-downloader API",
        version="1",
        description=(
            "Job-based API for downloading a movie from Einthusan and importing it "
            "into Radarr.\n\n"
            "Typical flow:\n"
            "1. `POST /api/v1/movies` with the Einthusan URL — returns a job in "
            "`resolving`, which becomes `awaiting_verification` once the TMDB match is known.\n"
            "2. Optionally `PATCH /api/v1/jobs/{job_id}` to correct the TMDB match.\n"
            "3. `POST /api/v1/jobs/{job_id}/download` to start the download and import.\n"
            "4. Poll `GET /api/v1/jobs/{job_id}` until `status` is `done` or `error`.\n\n"
            "Every request except `/api/v1/health` requires the `X-Api-Key` header. "
            "Errors share the shape `{\"error\": {\"code\": ..., \"message\": ...}}`, "
            "where `code` is stable and safe to branch on."
        ),
    )
    app.state.settings = settings or load_settings()
    app.state.job_store = JobStore()
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": "http_error", "message": str(exc.detail)}})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": str(exc)}})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": {"code": "internal", "message": str(exc)}})

    return app
