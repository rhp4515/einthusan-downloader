"""
Streamlit web UI for einthusan-dl.

Two-phase workflow:
  Phase 1 (sync, ~5 s): Login → fetch page → search TMDB → user picks match
  Phase 2 (background thread): Download → Radarr add/tag → manual import

Run locally:
    .venv/bin/streamlit run app.py
"""

import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# ── Path setup ──────────────────────────────���───────────────────────���──────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from einthusan_dl import (
    EinthusanClient,
    RadarrClient,
    detect_extension,
    safe_filename,
)

# ── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Einthusan Downloader",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Logging → queue bridge ───────────────────────────────────────────────────

class QueueLogHandler(logging.Handler):
    """Forwards log records to a queue so the Streamlit UI can display them."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        level = record.levelname  # DEBUG, INFO, WARNING, ERROR
        self.q.put({"type": "log", "level": level, "text": self.format(record)})


class ListLogHandler(logging.Handler):
    """Collects log records into a list for display after a sync operation."""

    def __init__(self, records: list, debug: bool = False):
        super().__init__()
        self.records = records
        self.setFormatter(logging.Formatter("%(message)s"))
        self.setLevel(logging.DEBUG if debug else logging.INFO)

    def emit(self, record):
        self.records.append({"level": record.levelname, "text": self.format(record)})


# ── Session state helpers ────────────────────────────────────────────────────

def _init_state():
    defaults: dict = {
        # Which screen to show
        "step": "input",        # input | preview | running | done | error

        # Phase 1 results (filled after preview)
        "movie_info": {},       # {title, year, language, video_url, page_url}
        "tmdb_results": [],     # list of Radarr/TMDB lookup dicts
        "tmdb_choice": 0,       # index into tmdb_results selected by user

        # Phase 2 live state (filled during download/import)
        "logs": [],             # list of {"level": ..., "text": ...}
        "dl_progress": (0, 0),  # (downloaded_bytes, total_bytes)
        "msg_queue": queue.Queue(),
        "result": None,         # final movie dict from Radarr
        "error": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _reset(to: str = "input"):
    keep = {"config"}  # keys to preserve across resets
    for k in list(st.session_state.keys()):
        if k not in keep:
            del st.session_state[k]
    _init_state()
    st.session_state.step = to


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load from .env then apply any sidebar overrides stored in session state."""
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    ov = st.session_state.get("config", {})

    def _get(key, default=""):
        return ov.get(key) or os.environ.get(key, default)

    return {
        "einthusan": {
            "username": _get("EINTHUSAN_USERNAME"),
            "password": _get("EINTHUSAN_PASSWORD"),
            "cookies": _get("EINTHUSAN_COOKIES"),
        },
        "radarr": {
            "url": _get("RADARR_URL", "http://localhost:7878").rstrip("/"),
            "api_key": _get("RADARR_API_KEY"),
            "root_folder": _get("RADARR_ROOT_FOLDER", "/data/media/movies"),
            "quality_profile_id": int(_get("RADARR_QUALITY_PROFILE_ID", "1")),
            "language_profile_id": int(_get("RADARR_LANGUAGE_PROFILE_ID", "1")),
        },
        "staging_host": _get("STAGING_DIR_HOST", "/data/media/manual_imports"),

    }


# ── Sidebar: settings ────────────────────────────────────────────────────────

def _sidebar():
    with st.sidebar:
        st.title("⚙️ Settings")
        st.caption("Values here override your .env file for this session.")

        cfg = _load_config()
        overrides = {}

        has_auth = cfg["einthusan"]["cookies"] or cfg["einthusan"]["username"]
        with st.expander("Einthusan", expanded=not has_auth):
            st.caption(
                "Enter your Einthusan username and password — a headless browser "
                "will log in automatically. Or paste a session cookie if you prefer."
            )
            overrides["EINTHUSAN_USERNAME"] = st.text_input(
                "Username / Email",
                value=cfg["einthusan"]["username"],
                key="si_user",
            )
            overrides["EINTHUSAN_PASSWORD"] = st.text_input(
                "Password",
                value=cfg["einthusan"]["password"],
                type="password",
                key="si_pass",
            )
            st.divider()
            st.caption("— or paste a session cookie to skip the browser login —")
            overrides["EINTHUSAN_COOKIES"] = st.text_input(
                "Session cookie  (sid=…)",
                value=cfg["einthusan"]["cookies"],
                placeholder="sid=MTc3...",
                help="Paste the full cookie string from your browser. "
                     "At minimum you need: sid=<value>. "
                     "You can also include: sid=X; _gorilla_csrf=Y; tid=Z",
                key="si_cookies",
            )

        with st.expander("Radarr", expanded=not cfg["radarr"]["api_key"]):
            overrides["RADARR_URL"] = st.text_input(
                "Radarr URL",
                value=cfg["radarr"]["url"],
                key="si_radarr_url",
            )
            overrides["RADARR_API_KEY"] = st.text_input(
                "API Key",
                value=cfg["radarr"]["api_key"],
                type="password",
                key="si_radarr_key",
            )
            overrides["RADARR_ROOT_FOLDER"] = st.text_input(
                "Root folder (inside container)",
                value=cfg["radarr"]["root_folder"],
                key="si_root",
            )
            overrides["RADARR_QUALITY_PROFILE_ID"] = st.text_input(
                "Quality profile ID",
                value=str(cfg["radarr"]["quality_profile_id"]),
                key="si_quality",
            )

        with st.expander("Paths"):
            overrides["STAGING_DIR_HOST"] = st.text_input(
                "Staging dir",
                value=cfg["staging_host"],
                key="si_staging_host",
            )

        # Persist non-empty overrides
        st.session_state["config"] = {k: v for k, v in overrides.items() if v}

        # Debug mode
        st.divider()
        st.session_state["debug_mode"] = st.checkbox(
            "Debug logging",
            value=st.session_state.get("debug_mode", False),
            help="Show detailed logs (field selectors, page HTML snippets, cookies) in the UI and terminal.",
        )

        # Radarr connectivity badge
        st.divider()
        radarr_url = overrides.get("RADARR_URL") or cfg["radarr"]["url"]
        radarr_key = overrides.get("RADARR_API_KEY") or cfg["radarr"]["api_key"]
        if radarr_key:
            rc = RadarrClient(radarr_url, radarr_key)
            if rc.ping():
                st.success("Radarr connected ✓", icon="🟢")
            else:
                st.error("Cannot reach Radarr", icon="🔴")
        else:
            st.warning("Radarr API key not set", icon="🟡")


# ── Drain the background queue into session_state ────────────────────────────

def _drain_queue():
    q: queue.Queue = st.session_state.msg_queue
    while not q.empty():
        try:
            msg = q.get_nowait()
        except queue.Empty:
            break
        t = msg.get("type")
        if t == "log":
            st.session_state.logs.append({"level": msg["level"], "text": msg["text"]})
        elif t == "progress":
            st.session_state.dl_progress = (msg["downloaded"], msg["total"])
        elif t == "done":
            st.session_state.result = msg.get("movie")
            st.session_state.step = "done"
        elif t == "error":
            st.session_state.error = msg["text"]
            st.session_state.step = "error"


# ── Background worker (Phase 2) ──────────────────────────────────────────────

def _background_import(
    cfg: dict,
    movie_info: dict,
    tmdb_result: dict,
    msg_queue: queue.Queue,
):
    """
    Runs in a daemon thread. All output goes through msg_queue so the
    Streamlit thread can display it safely.
    """
    def log(level: str, text: str):
        msg_queue.put({"type": "log", "level": level, "text": text})

    def on_progress(downloaded: int, total: int):
        msg_queue.put({"type": "progress", "downloaded": downloaded, "total": total})

    # Attach a log handler so every log.info() inside einthusan_dl flows to the UI.
    # Also set the level explicitly — without this the effective level defaults to
    # WARNING (inherited from the root logger) and INFO messages are silently dropped.
    debug = cfg.get("debug_mode", False)
    handler = QueueLogHandler(msg_queue)
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    dl_logger = logging.getLogger("einthusan_dl")
    dl_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    dl_logger.addHandler(handler)

    try:
        radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])

        # ── Resolve tag + language ──────────────��────────────────────────────
        log("INFO", "Resolving 'einthusan' tag in Radarr …")
        einthusan_tag_id = radarr.get_or_create_tag("einthusan")

        log("INFO", "Looking up Tamil language ID …")
        tamil_lang_id = radarr.get_language_id("Tamil")

        # ── Add or fetch movie in Radarr ─────────────────────────────────────
        tmdb_id = tmdb_result["tmdbId"]
        movie_title = tmdb_result["title"]
        movie_year = tmdb_result.get("year", movie_info.get("year", 0))

        existing = radarr.get_existing_movie(tmdb_id)
        if existing:
            log("INFO", f"Movie already in Radarr (id={existing['id']}): {movie_title}")
            radarr_movie = existing
            radarr.update_movie_tags(radarr_movie, [einthusan_tag_id])
        else:
            log("INFO", f"Adding '{movie_title} ({movie_year})' to Radarr …")
            radarr_movie = radarr.add_movie(
                tmdb_result,
                cfg["radarr"]["root_folder"],
                cfg["radarr"]["quality_profile_id"],
                cfg["radarr"]["language_profile_id"],
                tags=[einthusan_tag_id],
            )

        movie_id = radarr_movie["id"]

        # ── Ensure movie folder exists before import ──────────────────────────
        # Radarr creates the folder asynchronously after add_movie. Trigger a
        # RescanMovie command and wait for it to complete so the destination
        # folder exists when we later call manual_import_approve.
        log("INFO", "Waiting for Radarr to create the movie folder …")
        cmd_id = radarr.rescan_movie(movie_id)
        if cmd_id:
            radarr.wait_for_command(cmd_id, timeout=30)

        # ── Download ─────────────────────────────────────────────────────────
        video_url = movie_info["video_url"]
        ext = detect_extension(video_url)
        filename = safe_filename(movie_title, movie_year, ext)
        staging = Path(cfg["staging_host"])
        dest_path = staging / filename

        log("INFO", f"Downloading to: {dest_path}")
        client = EinthusanClient.__new__(EinthusanClient)
        client.session = movie_info["_session"]  # reuse authenticated session
        client.download(video_url, dest_path, on_progress=on_progress)

        # ── Manual import ────────────────────────────────────────────────────
        radarr_file_path = str(Path(cfg["staging_host"]) / dest_path.name)
        try:
            log("INFO", "Asking Radarr to analyse the staging folder …")
            import_items = radarr.manual_import_analyze(cfg["staging_host"], movie_id)
            matching = [i for i in import_items if Path(i["path"]).name == dest_path.name]

            if not matching:
                all_paths = [i.get("path", "") for i in import_items]
                log("WARNING",
                    f"Radarr could not see '{dest_path.name}' in the staging folder.\n"
                    f"Path checked: {cfg['staging_host']}\n"
                    f"Files Radarr did see: {all_paths or 'none'}\n"
                    "Import manually via Radarr UI → Movies → Manual Import.")
            else:
                radarr.manual_import_approve(
                    matching,
                    language_id=tamil_lang_id,
                    language_name="Tamil",
                    release_group="einthusan",
                    movie_id=movie_id,
                )
                log("INFO", "Import submitted to Radarr ✓")
                cmd_id = radarr.rescan_movie(movie_id)
                if cmd_id:
                    radarr.wait_for_command(cmd_id, timeout=60)
        except Exception as imp_exc:
            log("WARNING",
                f"Manual import API failed ({imp_exc}). "
                "Falling back to DownloadedMoviesScan command …")
            try:
                radarr.downloaded_movies_scan(radarr_file_path)
                log("INFO", "DownloadedMoviesScan triggered — Radarr will import the file automatically.")
                cmd_id = radarr.rescan_movie(movie_id)
                if cmd_id:
                    radarr.wait_for_command(cmd_id, timeout=60)
            except Exception as scan_exc:
                log("WARNING",
                    f"DownloadedMoviesScan also failed ({scan_exc}). "
                    "Import manually via Radarr UI → Movies → Manual Import.")

        msg_queue.put({"type": "done", "movie": {
            "title": movie_title,
            "year": movie_year,
            "radarr_id": movie_id,
            "tmdb_id": tmdb_id,
            "file": str(dest_path),
        }})

    except Exception as exc:
        msg_queue.put({"type": "error", "text": str(exc)})
    finally:
        dl_logger.removeHandler(handler)


# ── UI: Phase 1 — fetch preview ──────────────────────────────────────────────

def _run_preview(url: str, cfg: dict):
    """
    Synchronously: login → fetch page → TMDB lookup.
    Stores results in session_state and advances to 'preview'.
    """
    debug = st.session_state.get("debug_mode", False)
    phase1_logs: list = []

    dl_logger = logging.getLogger("einthusan_dl")
    list_handler = ListLogHandler(phase1_logs, debug=debug)
    # Also mirror to stderr so logs always appear in the terminal
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    stderr_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    dl_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    dl_logger.addHandler(list_handler)
    dl_logger.addHandler(stderr_handler)

    failed = False
    error_msg = ""

    try:
        with st.status("Fetching movie information …", expanded=True) as status:
            try:
                st.write("🔐 Logging in to Einthusan.tv …")
                client = EinthusanClient(
                    cfg["einthusan"]["username"],
                    cfg["einthusan"]["password"],
                    cfg["einthusan"]["cookies"],
                )
                client.login()
                st.write("✓ Logged in")

                st.write(f"📄 Fetching page: `{url}` …")
                movie_info = client.get_movie_info(url)
                # Stash the authenticated session so Phase 2 can reuse it
                movie_info["_session"] = client.session
                st.write(f"✓ Detected: **{movie_info['title']}** ({movie_info['year']})")

                st.write("🔍 Searching TMDB via Radarr …")
                radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
                results = radarr.lookup_movie(movie_info["title"], movie_info["year"])
                if not results:
                    status.update(label="Failed — no TMDB results", state="error")
                    failed = True
                    error_msg = "No TMDB results found. Try a different URL."
                else:
                    st.write(f"✓ Found {len(results)} result(s)")
                    st.session_state.movie_info = movie_info
                    st.session_state.tmdb_results = results
                    st.session_state.step = "preview"
                    status.update(label="Ready — confirm the details below", state="complete")

            except SystemExit:
                status.update(label="Failed", state="error")
                failed = True
                error_msg = "Login or extraction failed."
            except Exception as exc:
                status.update(label="Failed", state="error")
                failed = True
                error_msg = str(exc)
    finally:
        dl_logger.removeHandler(list_handler)
        dl_logger.removeHandler(stderr_handler)

    # Render error + logs OUTSIDE the st.status block so they are always visible.
    # (Content written inside a collapsed/errored status widget is hidden by Streamlit.)
    if failed:
        st.error(error_msg)

    if phase1_logs and (failed or debug):
        st.caption(f"Login log · {len(phase1_logs)} lines")
        _render_logs(phase1_logs)


# ── UI: step pages ────────────────────────────────────────────────────────────

def _page_input():
    st.title("🎬 Einthusan Downloader")
    st.caption("Download Tamil movies from Einthusan.tv and import them into Radarr / Jellyfin.")

    with st.form("url_form"):
        url = st.text_input(
            "Einthusan movie URL",
            placeholder="https://einthusan.tv/premium/movie/watch/XXXX/?lang=tamil",
        )
        submitted = st.form_submit_button("Fetch Movie Info →", type="primary", use_container_width=True)

    if submitted:
        url = url.strip()
        if not url or "einthusan.tv" not in url:
            st.error("Please enter a valid Einthusan movie URL.")
            return
        cfg = _load_config()
        has_einthusan_auth = cfg["einthusan"]["cookies"] or (
            cfg["einthusan"]["username"] and cfg["einthusan"]["password"]
        )
        if not has_einthusan_auth:
            st.error(
                "Einthusan auth not set. In the sidebar, either paste your **session cookie** "
                "(recommended) or enter your username + password."
            )
            return
        if not cfg["radarr"]["api_key"]:
            st.error("Radarr API key is not set. Fill it in the sidebar.")
            return
        _run_preview(url, cfg)
        st.rerun()


def _page_preview():
    info: dict = st.session_state.movie_info
    results: list = st.session_state.tmdb_results
    cfg = _load_config()

    st.title("🎬 Confirm Movie Details")

    # ── Einthusan info ───────────────���──────────────────────────────────────
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("Detected from page")
        st.markdown(f"**Title:** {info.get('title', '?')}")
        st.markdown(f"**Year:** {info.get('year', '?')}")
        st.markdown(f"**Language:** {info.get('language', 'Tamil')}")
        video_url = info.get("video_url", "")
        st.markdown(f"**Video URL:** `{video_url[:80]}{'…' if len(video_url) > 80 else ''}`")

    # ── TMDB match selection ─────────────────────────────────────────────────
    with col2:
        st.subheader("TMDB Match")
        options = [
            f"{r.get('title','?')} ({r.get('year','?')})  [tmdb={r.get('tmdbId','?')}]"
            for r in results[:8]
        ]
        choice = st.selectbox("Select correct movie", options, index=0, key="tmdb_select")
        st.session_state.tmdb_choice = options.index(choice)
        tmdb_id = results[st.session_state.tmdb_choice].get("tmdbId")
        if tmdb_id:
            st.link_button("Open on TMDB ↗", f"https://www.themoviedb.org/movie/{tmdb_id}")

    chosen = results[st.session_state.tmdb_choice]
    if chosen.get("overview"):
        st.caption(f"📝 {chosen['overview'][:200]}…" if len(chosen.get("overview","")) > 200 else chosen["overview"])

    # ── Destination info ─────────────────────────────────────────────────────
    ext = detect_extension(video_url)
    fname = safe_filename(chosen["title"], chosen.get("year", info.get("year", 0)), ext)
    st.info(f"📁 Will download to: `{cfg['staging_host']}/{fname}`", icon="💾")

    st.divider()
    col_go, col_back = st.columns([3, 1])
    with col_go:
        # on_click runs before the next render, so download_clicked=True is
        # already set when the button is re-drawn — making it instantly disabled.
        def _on_download_click():
            st.session_state.download_clicked = True

        clicked = st.button(
            "⬇️ Download & Import to Radarr",
            type="primary",
            use_container_width=True,
            disabled=st.session_state.get("download_clicked", False),
            on_click=_on_download_click,
        )
        if clicked and not st.session_state.get("_thread_started"):
            st.session_state._thread_started = True
            # Re-init the job state (keep movie_info and tmdb_results)
            st.session_state.logs = []
            st.session_state.dl_progress = (0, 0)
            st.session_state.msg_queue = queue.Queue()
            st.session_state.result = None
            st.session_state.error = ""
            st.session_state.step = "running"

            cfg["debug_mode"] = st.session_state.get("debug_mode", False)
            t = threading.Thread(
                target=_background_import,
                args=(cfg, info, chosen, st.session_state.msg_queue),
                daemon=True,
            )
            t.start()
            st.rerun()
    with col_back:
        if st.button("← Back", use_container_width=True):
            _reset("input")
            st.rerun()


def _render_logs(logs: list):
    """Render log lines inline — no expander, newest entries at the bottom."""
    if not logs:
        return
    ICON = {"INFO": "✅", "WARNING": "⚠️", "ERROR": "❌", "DEBUG": "🔹"}
    lines = [f"{ICON.get(e['level'], '•')}  {e['text']}" for e in logs]
    # Single code block so it renders instantly without creating N widgets
    st.code("\n".join(lines), language=None)


def _page_running():
    _drain_queue()

    logs: list = st.session_state.logs
    downloaded, total = st.session_state.dl_progress

    # ── Current step — always visible at the top ──────────────────────────────
    if logs:
        last = logs[-1]
        if last["level"] == "ERROR":
            st.error(last["text"])
        elif last["level"] == "WARNING":
            st.warning(last["text"])
        else:
            st.info(last["text"], icon="⏳")
    else:
        st.info("Starting up …", icon="⏳")

    # ── Download progress bar ─────────────────────────────────────────────────
    if total > 0:
        pct = min(downloaded / total, 1.0)
        dl_mb = downloaded / 1_048_576
        tot_mb = total / 1_048_576
        st.progress(pct, text=f"⬇️  {dl_mb:.1f} MB / {tot_mb:.1f} MB  ({pct*100:.0f}%)")
    elif downloaded > 0:
        st.progress(0.0, text=f"⬇️  {downloaded / 1_048_576:.1f} MB received …")

    # ── Full log — always expanded, no clicks needed ──────────────────────────
    if logs:
        st.caption(f"Activity log · {len(logs)} lines")
        _render_logs(logs)

    # ── Poll until the background thread signals done/error ───────────────────
    if st.session_state.step == "running":
        time.sleep(0.75)
        st.rerun()
    else:
        st.rerun()


def _page_done():
    result: dict = st.session_state.result or {}
    st.title("✅ Import Complete!")
    st.balloons()

    st.success(
        f"**{result.get('title','?')} ({result.get('year','?')})** "
        f"has been imported into Radarr and should appear in Jellyfin shortly.",
        icon="🎬",
    )

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Radarr ID", result.get("radarr_id", "?"))
        st.metric("TMDB ID", result.get("tmdb_id", "?"))
    with col2:
        st.markdown(f"**File:** `{result.get('file','?')}`")

    if st.session_state.logs:
        st.caption("Full log")
        _render_logs(st.session_state.logs)

    st.divider()
    if st.button("⬇️ Download Another Movie", type="primary"):
        _reset("input")
        st.rerun()


def _page_error():
    st.title("❌ Something went wrong")

    error_msg = st.session_state.error or "An unknown error occurred."
    st.error(error_msg)

    # Offer targeted hints for common failures
    if "ConnectTimeout" in error_msg or "timed out" in error_msg.lower():
        st.warning(
            "The download URL timed out. The CDN host may be unreachable from your server. "
            "The script automatically tries cdn1/cdn2/cdn3.einthusan.io — check the log below "
            "to see which host was used.",
            icon="⏱️",
        )
    elif "403" in error_msg or "401" in error_msg:
        st.warning(
            "Authentication error. Your session cookie may have expired — "
            "re-copy the `sid` cookie from your browser and update the sidebar.",
            icon="🔐",
        )

    # Logs always visible — no expander, no click needed
    st.divider()
    st.caption(f"Activity log · {len(st.session_state.logs)} lines")
    _render_logs(st.session_state.logs)

    st.divider()
    if st.button("← Try Again", type="primary"):
        _reset("input")
        st.rerun()


# ── Main ─────────────────────────────────���──────────────────────────────��─────

def main():
    _init_state()
    _sidebar()

    step = st.session_state.step
    if step == "input":
        _page_input()
    elif step == "preview":
        _page_preview()
    elif step == "running":
        _page_running()
    elif step == "done":
        _page_done()
    elif step == "error":
        _page_error()


if __name__ == "__main__":
    main()
