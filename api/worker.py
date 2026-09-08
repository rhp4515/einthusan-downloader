"""Background execution: resolve and download/import thread pools driving importer.py."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import importer
from api.jobs import Job, JobStore, Progress

resolve_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resolve")
download_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")

_PROGRESS_SAMPLE_SECONDS = 0.5


def submit_resolve(store: JobStore, core_config: dict, job: Job) -> None:
    resolve_pool.submit(_run_resolve, store, core_config, job.id)


def _run_resolve(store: JobStore, core_config: dict, job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        return
    try:
        resolved = importer.resolve_movie(core_config, job.einthusan_url)
    except importer.ImporterError as exc:
        store.update(job_id, state="resolve_failed", error={"code": exc.code, "message": str(exc)})
        return
    except Exception as exc:
        store.update(job_id, state="resolve_failed", error={"code": "internal", "message": str(exc)})
        return

    store.update(
        job_id,
        state="awaiting_verification",
        resolved=resolved,
        candidates=resolved.candidates,
        selected_tmdb_id=resolved.candidates[0].tmdb_id if resolved.candidates else None,
    )


def submit_download(store: JobStore, core_config: dict, job: Job) -> None:
    download_pool.submit(_run_download, store, core_config, job.id)


def _make_progress_callback(store: JobStore, job_id: str):
    # last_ts starts at 0.0 (not time.time()) so the very first progress
    # sample is never throttled away — it always reports immediately.
    state = {"last_ts": 0.0, "last_bytes": 0}

    def on_progress(downloaded: int, total: int) -> None:
        job = store.get(job_id)
        if job is None:
            return
        if job.cancelled:
            raise importer.DownloadCancelled("Job was cancelled")

        now = time.time()
        elapsed = now - state["last_ts"]
        if elapsed < _PROGRESS_SAMPLE_SECONDS and downloaded < total:
            return

        delta_bytes = downloaded - state["last_bytes"]
        speed_bps = delta_bytes / elapsed if elapsed > 0 else 0.0
        remaining = max(total - downloaded, 0)
        eta_seconds = remaining / speed_bps if speed_bps > 0 else None
        percent = (downloaded / total * 100) if total else 0.0

        new_state = "importing" if total and downloaded >= total else "downloading"
        store.update(
            job_id,
            state=new_state,
            progress=Progress(
                downloaded_bytes=downloaded,
                total_bytes=total,
                percent=round(percent, 2),
                speed_bps=round(speed_bps, 2),
                eta_seconds=round(eta_seconds, 1) if eta_seconds is not None else None,
            ),
        )
        state["last_ts"] = now
        state["last_bytes"] = downloaded

    return on_progress


def _run_download(store: JobStore, core_config: dict, job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        return

    store.update(job_id, state="downloading")
    candidate = next((c for c in job.candidates if c.tmdb_id == job.selected_tmdb_id), None)
    if candidate is None:
        store.update(job_id, state="error", error={"code": "internal", "message": "No candidate selected"})
        return

    on_progress = _make_progress_callback(store, job_id)
    try:
        dest = importer.run_download_and_import(
            core_config,
            resolved=job.resolved,
            candidate=candidate,
            radarr_movie_id=job.radarr_movie_id,
            on_progress=on_progress,
        )
    except importer.DownloadCancelled:
        store.update(job_id, state="error", error={"code": "cancelled", "message": "Job was cancelled"})
        return
    except importer.ImporterError as exc:
        store.update(job_id, state="error", error={"code": exc.code, "message": str(exc)})
        return
    except Exception as exc:
        store.update(job_id, state="error", error={"code": "internal", "message": str(exc)})
        return

    store.update(
        job_id,
        state="done",
        result={"file": str(dest), "radarr_movie_id": job.radarr_movie_id, "tmdb_id": candidate.tmdb_id},
    )
