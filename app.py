"""
Streamlit web UI for einthusan-dl.

Thin HTTP client over the einthusan-downloader API (see api/). All
orchestration (login, scraping, Radarr, download, import) lives server-side
in importer.py / api/worker.py; this file only renders job state and lets the
user verify the TMDB match before triggering the download.

Run locally (API must already be running — see api/__main__.py):
    .venv/bin/streamlit run app.py
"""

import os
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from api_client import EinthusanApiClient, EinthusanApiError

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

st.set_page_config(
    page_title="Einthusan Downloader",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

POLL_SECONDS = 1.5


def _init_state():
    st.session_state.setdefault("job_id", None)
    st.session_state.setdefault("step", "input")
    st.session_state.setdefault("error", "")
    st.session_state.setdefault("api_base", os.environ.get("EINTHUSAN_API_BASE", "http://localhost:8500"))
    st.session_state.setdefault("api_key", os.environ.get("EINTHUSAN_API_KEY", ""))


def _client() -> EinthusanApiClient:
    return EinthusanApiClient(st.session_state["api_base"], st.session_state["api_key"])


def _sidebar():
    st.sidebar.header("Connection")
    st.session_state["api_base"] = st.sidebar.text_input("API base URL", value=st.session_state["api_base"])
    st.session_state["api_key"] = st.sidebar.text_input("API key", value=st.session_state["api_key"], type="password")


def _reset():
    st.session_state["job_id"] = None
    st.session_state["step"] = "input"
    st.session_state["error"] = ""


def _fail(exc: EinthusanApiError):
    st.session_state["error"] = f"{exc.code}: {exc.message}"
    st.session_state["step"] = "error"


def _page_input():
    st.title("🎬 Einthusan Downloader")
    st.caption("Download Tamil movies from Einthusan.tv and import them into Radarr / Jellyfin.")

    with st.form("url_form"):
        url = st.text_input("Einthusan movie URL", placeholder="https://einthusan.tv/movie/watch/...")
        submitted = st.form_submit_button("Fetch details")

    if submitted and url:
        try:
            job = _client().create_job(url)
        except EinthusanApiError as exc:
            _fail(exc)
        else:
            st.session_state["job_id"] = job.id
            st.session_state["step"] = "resolving"
        st.rerun()


def _page_resolving():
    st.title("🔍 Looking up movie details …")
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return

    if job.state == "resolve_failed":
        message = job.error.get("message", "Resolve failed") if job.error else "Resolve failed"
        st.session_state["error"] = message
        st.session_state["step"] = "error"
        st.rerun()
        return

    if job.state == "awaiting_verification":
        st.session_state["step"] = "preview"
        st.rerun()
        return

    with st.spinner("Fetching page and searching TMDB …"):
        time.sleep(POLL_SECONDS)
    st.rerun()


def _page_preview():
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return
    st.title("✅ Confirm the match")

    options = {f"{c.title} ({c.year}) — tmdb:{c.tmdb_id}": c.tmdb_id for c in job.candidates}
    labels = list(options.keys())
    default_label = next((label for label, tid in options.items() if tid == job.selected_tmdb_id), labels[0])
    choice = st.radio("TMDB match", labels, index=labels.index(default_label))
    chosen_tmdb_id = options[choice]

    for c in job.candidates:
        if c.tmdb_id == chosen_tmdb_id:
            if c.poster_url:
                st.image(c.poster_url, width=200)
            st.markdown(f"[View on TMDB]({c.tmdb_url})")

    col1, col2 = st.columns(2)
    if col1.button("Confirm and download", type="primary"):
        try:
            if chosen_tmdb_id != job.selected_tmdb_id:
                _client().patch_job(job.id, chosen_tmdb_id)
            _client().start_download(job.id)
        except EinthusanApiError as exc:
            _fail(exc)
        else:
            st.session_state["step"] = "running"
        st.rerun()

    if col2.button("Cancel"):
        try:
            _client().delete_job(job.id)
        except EinthusanApiError:
            pass
        _reset()
        st.rerun()


def _page_running():
    st.title("⏳ Downloading and importing …")
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return

    if job.state == "error":
        message = job.error.get("message", "Import failed") if job.error else "Import failed"
        st.session_state["error"] = message
        st.session_state["step"] = "error"
        st.rerun()
        return

    if job.state == "done":
        st.session_state["step"] = "done"
        st.rerun()
        return

    if job.progress and job.progress.get("total_bytes"):
        st.progress(min(job.progress["percent"] / 100, 1.0))
        st.caption(
            f"{job.progress['downloaded_bytes'] / 1e6:.1f} MB / "
            f"{job.progress['total_bytes'] / 1e6:.1f} MB — "
            f"{job.progress['speed_bps'] / 1e6:.2f} MB/s"
        )
    else:
        st.spinner("Working …")

    time.sleep(POLL_SECONDS)
    st.rerun()


def _page_done():
    st.title("🎉 Done!")
    try:
        job = _client().get_job(st.session_state["job_id"])
    except EinthusanApiError as exc:
        _fail(exc)
        st.rerun()
        return

    if job.result:
        st.success(f"Imported: {job.result['file']}")
        st.write(f"Radarr movie ID: {job.result['radarr_movie_id']}")
    if st.button("Import another movie"):
        _reset()
        st.rerun()


def _page_error():
    st.title("❌ Something went wrong")
    st.error(st.session_state.get("error", "Unknown error"))
    if st.button("Start over"):
        _reset()
        st.rerun()


def main():
    _init_state()
    _sidebar()
    step = st.session_state["step"]
    pages = {
        "input": _page_input,
        "resolving": _page_resolving,
        "preview": _page_preview,
        "running": _page_running,
        "done": _page_done,
    }
    pages.get(step, _page_error)()


if __name__ == "__main__":
    main()
