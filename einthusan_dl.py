#!/usr/bin/env python3
"""
einthusan-dl: Download Tamil movies from Einthusan.tv and import into Radarr/Jellyfin

Usage:
    python3 einthusan_dl.py <einthusan_movie_url> [options]

Examples:
    python3 einthusan_dl.py https://einthusan.tv/movie/watch/12345/
    python3 einthusan_dl.py https://einthusan.tv/movie/watch/12345/ --debug
    python3 einthusan_dl.py https://einthusan.tv/movie/watch/12345/ --download-only
    python3 einthusan_dl.py https://einthusan.tv/movie/watch/12345/ --radarr-only /path/to/file.mp4
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
import base64
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from dotenv import load_dotenv

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load configuration from .env file (must be in the same directory as this script)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        log.error(
            f".env file not found at {env_path}\n"
            f"Copy .env.example to .env and fill in your credentials."
        )
        sys.exit(1)
    load_dotenv(env_path)

    has_cookies = bool(os.environ.get("EINTHUSAN_COOKIES"))
    has_credentials = bool(os.environ.get("EINTHUSAN_USERNAME") and os.environ.get("EINTHUSAN_PASSWORD"))
    if not has_cookies and not has_credentials:
        log.error(
            "Einthusan auth not configured.\n"
            "Set either EINTHUSAN_COOKIES (recommended) or "
            "EINTHUSAN_USERNAME + EINTHUSAN_PASSWORD in .env."
        )
        sys.exit(1)

    required = [
        "RADARR_URL", "RADARR_API_KEY",
        "RADARR_ROOT_FOLDER", "STAGING_DIR_HOST",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error(f"Missing required config keys in .env: {', '.join(missing)}")
        sys.exit(1)

    return {
        "einthusan": {
            "username": os.environ.get("EINTHUSAN_USERNAME", ""),
            "password": os.environ.get("EINTHUSAN_PASSWORD", ""),
            "cookies": os.environ.get("EINTHUSAN_COOKIES", ""),
            "base_url": "https://einthusan.tv",
        },
        "radarr": {
            "url": os.environ["RADARR_URL"].rstrip("/"),
            "api_key": os.environ["RADARR_API_KEY"],
            "root_folder": os.environ["RADARR_ROOT_FOLDER"],
            "quality_profile_id": int(os.environ.get("RADARR_QUALITY_PROFILE_ID", "1")),
            "language_profile_id": int(os.environ.get("RADARR_LANGUAGE_PROFILE_ID", "1")),
        },
        "staging_host": os.environ["STAGING_DIR_HOST"],
    }


# ── Einthusan client ──────────────────────────────────────────────────────────

class EinthusanClient:
    """Handles login and video URL extraction from Einthusan.tv."""

    BASE_URL = "https://einthusan.tv"

    # Tried in order when doing form-based login
    _LOGIN_CANDIDATES = [
        "https://einthusan.tv/login/?lang=tamil",
        "https://einthusan.tv/account/signin/?lang=tamil",
        "https://einthusan.tv/account/login/?lang=tamil",
        "https://einthusan.tv/signin/?lang=tamil",
    ]

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, username: str = "", password: str = "", cookies: str = ""):
        self.username = username
        self.password = password
        self._raw_cookies = cookies
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self._logged_in = False

    # ── Authentication ────────────────────────────────────────────────────────

    def login(self) -> None:
        """
        Authenticate with Einthusan.tv.

        Three methods, tried in order:
          1. Cookie string   — set EINTHUSAN_COOKIES in .env (fastest)
          2. Browser login   — headless Chromium via Playwright (recommended when
                               using username + password; handles any JS auth flow)
          3. Form login      — plain HTTP POST fallback if Playwright is unavailable
        """
        if self._raw_cookies:
            self._login_with_cookies()
        elif self.username and self.password:
            if _PLAYWRIGHT_AVAILABLE:
                self._browser_login()
            else:
                log.warning(
                    "Playwright is not installed — falling back to form login. "
                    "Run: playwright install chromium"
                )
                self._form_login()
        else:
            log.error(
                "Einthusan auth not configured. "
                "Set EINTHUSAN_USERNAME + EINTHUSAN_PASSWORD, or EINTHUSAN_COOKIES."
            )
            sys.exit(1)

    def _login_with_cookies(self) -> None:
        """
        Inject a browser cookie string directly into the session.

        How to get your cookies:
          1. Log in to einthusan.tv in Chrome/Firefox
          2. Open DevTools (F12) → Application → Cookies → einthusan.tv
          3. Copy the 'sid' value (the session cookie)
          4. Set EINTHUSAN_COOKIES=sid=<value> in your .env
             (optionally include other cookies: sid=X; _gorilla_csrf=Y; tid=Z)
        """
        log.info("Authenticating via session cookies …")
        for part in self._raw_cookies.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                self.session.cookies.set(k.strip(), v.strip(), domain="einthusan.tv")
        log.info("Session cookies set — skipping form login.")
        self._logged_in = True

    def _browser_login(self) -> None:
        """
        Login via Einthusan's arc65.page event bus.

        The site's login is not a form POST.  UILogin registers a 'Login' handler
        on the arc65.page event bus and handles CSRF internally via arc65.page.id.
        UILogin itself is never a global — it lives inside a closure — so we call
        arc65.page.send('Login', ...) directly.
        """
        login_url = "https://einthusan.tv/login/?lang=tamil"
        log.info("Loading login page via headless browser …")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=self.HEADERS["User-Agent"])
            page = ctx.new_page()

            try:
                page.goto(login_url, timeout=30_000)
            except PlaywrightTimeout:
                browser.close()
                raise RuntimeError("Timed out loading Einthusan login page.")

            page.wait_for_load_state("load")
            log.debug(f"Page loaded: {page.url!r}")

            # Wait for the arc65.page event bus (always present; UILogin registers on it)
            try:
                page.wait_for_function(
                    "typeof arc65 !== 'undefined' && typeof arc65.page !== 'undefined' "
                    "&& typeof arc65.page.send === 'function'",
                    timeout=10_000,
                )
            except PlaywrightTimeout:
                browser.close()
                raise RuntimeError(
                    "arc65.page event bus not found after 10 s — site JS may have changed."
                )

            # Send credentials and wait for the /ajax/login/ response in one step.
            log.info("Sending login credentials via browser …")
            try:
                with page.expect_response(
                    lambda r: "einthusan.tv/ajax/login" in r.url,
                    timeout=15_000,
                ) as resp_info:
                    page.evaluate(
                        "([e, p]) => arc65.page.send('Login', { Email: e, Password: p })",
                        [self.username, self.password],
                    )
            except PlaywrightTimeout:
                browser.close()
                raise RuntimeError("No response from /ajax/login/ within 15 s.")

            resp = resp_info.value
            try:
                data = resp.json()
            except Exception:
                data = {}
            log.debug(f"Login API: HTTP {resp.status} → {data!r}")

            # The API always returns HTTP 200; success/failure is in the JSON body.
            if data.get("Event") == "UserMessage" and data.get("Data", {}).get("Err"):
                browser.close()
                msg = data["Data"].get("Message", "unknown error")
                raise RuntimeError(
                    f"Einthusan login failed: {msg}. "
                    "Check your EINTHUSAN_USERNAME / EINTHUSAN_PASSWORD."
                )

            cookies = ctx.cookies()
            einthusan_cookies = [c["name"] for c in cookies if "einthusan" in (c.get("domain") or "")]
            log.debug(f"Einthusan cookies acquired: {einthusan_cookies}")
            browser.close()

        for c in cookies:
            self.session.cookies.set(c["name"], c["value"],
                                     domain=c.get("domain", "einthusan.tv"))

        log.info("Browser login successful.")
        self._logged_in = True

    def _form_login(self) -> None:
        """Find the login page dynamically and POST credentials."""
        log.info("Logging in to Einthusan.tv …")

        login_url = self._find_login_url()
        if not login_url:
            log.error(
                "Could not find Einthusan login page.\n"
                "The site may have changed its login URL.\n"
                "Use cookie-based auth instead: set EINTHUSAN_COOKIES=sid=<value> in .env.\n"
                "  How to get it: DevTools (F12) → Application → Cookies → einthusan.tv → copy 'sid'"
            )
            sys.exit(1)

        log.debug(f"Login URL: {login_url}")
        resp = self.session.get(login_url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        csrf_token, csrf_field = self._extract_csrf(soup)
        log.debug(f"CSRF: field={csrf_field}")

        payload = {csrf_field: csrf_token, "username": self.username, "password": self.password}
        resp = self.session.post(
            login_url,
            data=payload,
            headers={"Referer": login_url},
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()

        if self._is_logged_in(resp):
            log.info("Login successful.")
            self._logged_in = True
        else:
            log.error(
                "Login failed — wrong credentials or the login form has changed.\n"
                f"Final URL: {resp.url}\n"
                "Switch to cookie-based auth: set EINTHUSAN_COOKIES=sid=<value> in .env."
            )
            log.debug(f"Response snippet:\n{resp.text[:2000]}")
            sys.exit(1)

    def _find_login_url(self) -> str | None:
        """
        Try known login URL candidates; return the first that serves an actual form.
        Handles sites that return 307 → /e404/ for moved endpoints.
        """
        for url in self._LOGIN_CANDIDATES:
            try:
                resp = self.session.get(url, timeout=15, allow_redirects=True)
                # Only accept if the final URL still looks like a login page
                # (not a 404 redirect) and contains a <form>
                if resp.status_code == 200 and "e404" not in resp.url and "<form" in resp.text.lower():
                    return resp.url
            except Exception as e:
                log.debug(f"Login candidate {url} failed: {e}")
        return None

    def _extract_csrf(self, soup: BeautifulSoup) -> tuple[str, str]:
        """
        Return (token_value, field_name) for whichever CSRF scheme the page uses.

        Einthusan switched from Django (csrfmiddlewaretoken) to a Go backend
        (gorilla/csrf) — both are handled here.
        """
        # gorilla/csrf hidden input
        for name in ("gorilla.csrf.Token", "csrf_token", "_csrf", "csrfmiddlewaretoken"):
            tag = soup.find("input", {"name": name})
            if tag and tag.get("value"):
                return tag["value"], name

        # gorilla/csrf meta tag (some templates use this)
        meta = soup.find("meta", {"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"], "X-CSRF-Token"

        # Cookie-based fallback (Django csrftoken cookie)
        token = self.session.cookies.get("csrftoken")
        if token:
            return token, "csrfmiddlewaretoken"

        raise RuntimeError(
            "Could not find CSRF token on login page.\n"
            "Use cookie-based auth instead: set EINTHUSAN_COOKIES=sid=<value> in .env."
        )

    def _is_logged_in(self, resp: requests.Response) -> bool:
        indicators = ["account/logout", "my-account", "einthusan-plus", "logout", "/signout"]
        return any(i in resp.text.lower() for i in indicators)

    # ── Movie page scraping ───────────────────────────────────────────────────

    def get_movie_info(self, url: str) -> dict:
        """
        Fetch the movie page and extract:
          - title (str)
          - year (int)
          - video_url (str)   — direct MP4 URL
          - language (str)
        """
        if not self._logged_in:
            self.login()

        log.info(f"Fetching movie page: {url}")
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()

        if "login" in resp.url.lower():
            log.error("Redirected to login page — session may have expired.")
            sys.exit(1)

        soup = BeautifulSoup(resp.text, "lxml")
        log.debug("Page fetched successfully.")

        title, year = self._extract_title_year(soup, url)
        language = self._extract_language(soup, url)
        video_url = self._extract_video_url(soup, resp.text, url)

        return {
            "title": title,
            "year": year,
            "language": language,
            "video_url": video_url,
            "page_url": url,
        }

    def _extract_title_year(self, soup: BeautifulSoup, page_url: str) -> tuple[str, int]:
        """Return (title, year) from the movie page."""
        title = ""
        year = 0

        # Method 1: data-content-title on #UIVideoPlayer (e.g. "Sabdham")
        player = soup.find(id="UIVideoPlayer")
        if player and player.get("data-content-title"):
            title = player["data-content-title"].strip()
            log.debug(f"Title from data-content-title: {title}")

        # Method 2: <title> tag — "Sabdham (2025) Tamil in HD - Einthusan"
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            # Extract year from the title tag first (most reliable source)
            m_year = re.search(r"\((\d{4})\)", raw)
            if m_year:
                year = int(m_year.group(1))
            # Use title from tag only if not already found via data-content-title
            if not title:
                # Strip everything after the year or after " - Einthusan"
                raw_title = re.split(r"\s*[\|–-]\s*Einthusan", raw, flags=re.I)[0].strip()
                # Also strip "(YYYY) Tamil in HD" suffix if present
                raw_title = re.sub(r"\s*\(\d{4}\).*$", "", raw_title).strip()
                title = raw_title
                log.debug(f"Title from <title> tag: {title}")

        # Method 3: fallback heading selectors
        if not title:
            for selector in ["h3.film-title", "h1.film-title", "#UIMovieSummary h3", ".film-title"]:
                el = soup.select_one(selector)
                if el:
                    title = el.get_text(strip=True)
                    log.debug(f"Title from selector '{selector}': {title}")
                    break

        title = title or "Unknown"

        log.info(f"Detected title: '{title}', year: {year or 'unknown'}")
        return title, year

    def _extract_language(self, soup: BeautifulSoup, page_url: str) -> str:
        """Best-effort language detection from URL or page."""
        url_lower = page_url.lower()
        for lang in ("tamil", "hindi", "telugu", "malayalam", "kannada"):
            if lang in url_lower:
                return lang.capitalize()
        # Check page body
        for lang in ("Tamil", "Hindi", "Telugu", "Malayalam", "Kannada"):
            if lang in str(soup):
                return lang
        return "Tamil"  # default for einthusan

    # ── CDN resolution ────────────────────────────────────────────────────────

    def _cdn_hosts_from_page(self, soup: BeautifulSoup) -> list[str]:
        """
        Decode the data-ejpingables attribute to get the CDN hostnames.
        Falls back to known hostnames if decoding fails.
        """
        player = soup.find(id="UIVideoPlayer") or soup.find(attrs={"data-ejpingables": True})
        if player:
            raw = player.get("data-ejpingables", "")
            if raw:
                # Attribute sometimes has a stray trailing char — keep only base64 chars
                clean = re.sub(r"[^A-Za-z0-9+/=]", "", raw)
                # Ensure correct padding
                clean += "=" * (-len(clean) % 4)
                try:
                    decoded = base64.b64decode(clean).decode("utf-8", errors="ignore")
                    urls = json.loads(decoded)
                    hosts = [urllib.parse.urlparse(u).netloc for u in urls if u]
                    hosts = [h for h in hosts if h and "einthusan" in h]
                    if hosts:
                        log.debug(f"CDN hosts from page: {hosts}")
                        return hosts
                except Exception as e:
                    log.debug(f"Could not decode ejpingables: {e}")
        return ["cdn1.einthusan.io", "cdn2.einthusan.io", "cdn3.einthusan.io"]

    def _resolve_to_cdn(self, url: str, soup: BeautifulSoup) -> str:
        """
        The page embeds raw IP addresses in data-mp4-link, but those IPs are
        often unreachable directly (firewall, geo-block).  Replace the IP with
        a proper cdn*.einthusan.io hostname — the signed tokens are path-based
        and work regardless of which CDN hostname is used.
        """
        parsed = urllib.parse.urlparse(url)
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", parsed.netloc):
            return url  # already a hostname, nothing to do

        log.debug(f"Raw IP detected ({parsed.netloc}) — probing CDN hostnames …")
        path_qs = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        cdn_hosts = self._cdn_hosts_from_page(soup)

        for host in cdn_hosts:
            cdn_url = f"https://{host}{path_qs}"
            try:
                resp = self.session.head(cdn_url, timeout=8, allow_redirects=True)
                if resp.status_code in (200, 206, 301, 302):
                    log.info(f"CDN resolved: {parsed.netloc} → {host}")
                    return cdn_url
            except Exception as e:
                log.debug(f"  {host} unreachable: {e}")

        # All probes failed — use cdn1 and hope for the best
        fallback = f"https://{cdn_hosts[0]}{path_qs}"
        log.warning(f"All CDN probes timed out; using {cdn_hosts[0]} as fallback")
        return fallback

    def _extract_video_url(self, soup: BeautifulSoup, html: str, page_url: str) -> str:
        """
        Try multiple methods to extract the direct MP4/video URL.
        Logs each attempt so failures are easy to debug.

        Confirmed page structure (as of 2025):
          <section id="UIVideoPlayer"
                   data-mp4-link="https://<cdn-ip>/etv/content/<id>.mp4?e=<exp>&md5=<sig>&p=priority"
                   data-hls-link="https://<cdn-ip>/etv/content/<id>.mp4.m3u8?...">

        The IPs in data-mp4-link are raw CDN IPs that may be unreachable; they
        are automatically swapped for cdn*.einthusan.io hostnames.
        """

        # ── Method 1: data-mp4-link on #UIVideoPlayer (primary, confirmed) ────
        player = soup.find(id="UIVideoPlayer")
        if player:
            mp4_link = player.get("data-mp4-link", "").strip()
            if mp4_link:
                mp4_link = self._resolve_to_cdn(mp4_link, soup)
                log.info("[Method 1] Found video URL via #UIVideoPlayer data-mp4-link")
                return mp4_link
            log.debug("[Method 1] #UIVideoPlayer found but data-mp4-link is empty")

        # ── Method 2: any element carrying data-mp4-link ──────────────────────
        el = soup.find(attrs={"data-mp4-link": True})
        if el:
            mp4_link = el.get("data-mp4-link", "").strip()
            if mp4_link:
                mp4_link = self._resolve_to_cdn(mp4_link, soup)
                log.info(f"[Method 2] Found video URL via data-mp4-link on <{el.name}>")
                return mp4_link

        # ── Method 3: data-hls-link fallback (convert m3u8 hint to mp4) ───────
        player = player or soup.find(attrs={"data-hls-link": True})
        if player:
            hls_link = player.get("data-hls-link", "").strip()
            if hls_link:
                hls_link = self._resolve_to_cdn(hls_link, soup)
                # Try converting the HLS URL to an MP4 URL
                mp4_from_hls = re.sub(r"\.mp4\.m3u8", ".mp4", hls_link)
                if mp4_from_hls != hls_link:
                    log.info("[Method 3] Derived MP4 URL from data-hls-link")
                    return mp4_from_hls
                log.info("[Method 3] Falling back to HLS streaming URL")
                return hls_link

        # ── Method 4: JS variables / JSON blobs in page source ────────────────
        for pattern in [
            r'"mp4_link"\s*:\s*"([^"]+)"',
            r'"videoURL"\s*:\s*"([^"]+)"',
            r'"file"\s*:\s*"([^"]+\.mp4[^"]*)"',
            r'EinthusanData\s*=\s*(\{[^}]+\})',
        ]:
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m:
                raw = m.group(1)
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw)
                        for key in ("mp4_link", "videoURL", "file", "url"):
                            if key in obj and obj[key]:
                                log.info(f"[Method 4] Found video URL via JS object key '{key}'")
                                return obj[key]
                    except json.JSONDecodeError:
                        pass
                else:
                    log.info("[Method 4] Found video URL via JS regex pattern")
                    return raw

        # ── Method 5: <source> tag inside <video> ─────────────────────────────
        video_tag = soup.find("video")
        if video_tag:
            source = video_tag.find("source")
            if source and source.get("src"):
                log.info("[Method 5] Found video URL via <source> tag")
                return source["src"]

        # ── Method 6: bare .mp4 URLs in page HTML ─────────────────────────────
        mp4_matches = re.findall(r'(https?://[^\s\'"<>]+\.mp4[^\s\'"<>]*)', html)
        if mp4_matches:
            log.info(f"[Method 6] Found {len(mp4_matches)} .mp4 URL(s) in page source")
            return mp4_matches[0]

        # ── Give up ────────────────────────────────────────────────────────────
        log.error(
            "Could not extract video URL from page.\n"
            "Run with --debug to see the full page source.\n"
            "Einthusan may have changed their page structure."
        )
        log.debug(f"Page HTML snippet:\n{html[:3000]}")
        sys.exit(1)

    def _resolve_video_url(self, raw_url: str, page_url: str) -> str | None:
        """
        Turn a relative or token URL into a final direct download URL.
        If the URL redirects or returns a JSON response with a video URL, follow it.
        """
        if not raw_url:
            return None

        # Make absolute
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        elif raw_url.startswith("/"):
            base = urllib.parse.urlparse(page_url)
            raw_url = f"{base.scheme}://{base.netloc}{raw_url}"

        # If it already looks like a direct video file, return as-is
        if re.search(r"\.(mp4|mkv|m3u8|ts)(\?|$)", raw_url, re.I):
            return raw_url

        # Try fetching it — it might redirect or return JSON
        log.debug(f"Resolving indirect URL: {raw_url}")
        try:
            resp = self.session.get(raw_url, timeout=15, allow_redirects=True, stream=True)
            # If final URL is a video file
            if re.search(r"\.(mp4|mkv|m3u8|ts)(\?|$)", resp.url, re.I):
                return resp.url
            # Content-Type video
            ct = resp.headers.get("Content-Type", "")
            if "video" in ct or "octet-stream" in ct:
                return resp.url
            # JSON response with a url key
            if "json" in ct:
                data = resp.json()
                for key in ("url", "mp4_url", "videoUrl", "src", "file"):
                    if key in data and data[key]:
                        return self._resolve_video_url(data[key], page_url)
        except Exception as e:
            log.debug(f"Could not resolve {raw_url}: {e}")
        return None

    def _try_api_endpoints(self, content_id: str) -> str | None:
        """Probe known Einthusan API patterns for a given content ID."""
        candidates = [
            f"https://einthusan.tv/api/v1/movie/{content_id}/",
            f"https://einthusan.tv/media/watch/{content_id}/",
        ]
        for url in candidates:
            log.debug(f"Probing API endpoint: {url}")
            try:
                resp = self.session.get(url, timeout=10)
                if resp.status_code == 200:
                    if "json" in resp.headers.get("Content-Type", ""):
                        data = resp.json()
                        for key in ("url", "mp4_url", "videoUrl", "src", "file", "mp4_link"):
                            if key in data and data[key]:
                                return data[key]
                    elif re.search(r"\.(mp4|m3u8)(\?|$)", resp.url, re.I):
                        return resp.url
            except Exception:
                pass
        return None

    # ── Download ──────────────────────────────────────────────────────────────

    def download(
        self,
        video_url: str,
        dest_path: Path,
        on_progress=None,
        chunk_size: int = 1024 * 1024,
    ) -> Path:
        """
        Download the video file with a progress bar.
        Resumes partial downloads if the server supports Range requests.

        on_progress: optional callable(bytes_downloaded: int, total_bytes: int)
            When provided, tqdm is skipped and this callback is used for progress
            reporting (e.g. to feed a Streamlit progress bar).
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        existing_size = dest_path.stat().st_size if dest_path.exists() else 0

        headers = {}
        if existing_size:
            headers["Range"] = f"bytes={existing_size}-"
            log.info(f"Resuming download from {existing_size / 1e6:.1f} MB")

        log.info(f"Downloading: {video_url}")
        log.info(f"Destination: {dest_path}")

        resp = self.session.get(video_url, headers=headers, stream=True, timeout=60)

        if existing_size and resp.status_code == 416:
            # Stale partial file — CDN token may have rotated; start over
            log.warning("Range not satisfiable (416) — removing partial file and restarting.")
            resp.close()
            dest_path.unlink(missing_ok=True)
            existing_size = 0
            resp = self.session.get(video_url, stream=True, timeout=60)

        # Server may not support range — start over
        if existing_size and resp.status_code == 200:
            log.debug("Server does not support resuming; starting over.")
            existing_size = 0

        if resp.status_code not in (200, 206):
            resp.raise_for_status()

        total = int(resp.headers.get("Content-Length", 0)) + existing_size
        downloaded = existing_size
        mode = "ab" if existing_size else "wb"

        if on_progress:
            with open(dest_path, mode) as fh:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    on_progress(downloaded, total)
        else:
            with open(dest_path, mode) as fh, tqdm(
                total=total or None,
                initial=existing_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=dest_path.name,
                ncols=80,
            ) as bar:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    fh.write(chunk)
                    bar.update(len(chunk))

        log.info(f"Download complete: {dest_path} ({dest_path.stat().st_size / 1e6:.1f} MB)")
        return dest_path


# ── Radarr client ─────────────────────────────────────────────────────────────

class RadarrClient:
    """Wraps the Radarr v3 API for movie management and manual import."""

    def __init__(self, url: str, api_key: str):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
        })

    def _get(self, path: str, **params) -> requests.Response:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, payload: dict) -> requests.Response:
        resp = self.session.post(f"{self.base}{path}", json=payload, timeout=30)
        if not resp.ok:
            body = resp.text[:500]
            log.error(f"Radarr POST {path} → HTTP {resp.status_code}: {body}")
        resp.raise_for_status()
        return resp

    # ── Health check ──────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            self._get("/api/v3/system/status")
            return True
        except Exception as e:
            log.error(f"Cannot reach Radarr at {self.base}: {e}")
            return False

    # ── Movie lookup ──────────────────────────────────────────────────────────

    def lookup_movie(self, title: str, year: int = 0) -> list[dict]:
        """Search Radarr's TMDB lookup for a movie by title (and optional year)."""
        query = f"{title} {year}" if year else title
        log.info(f"Searching Radarr/TMDB for: '{query}'")
        resp = self._get("/api/v3/movie/lookup", term=query)
        results = resp.json()
        log.debug(f"Got {len(results)} results from TMDB lookup")
        return results

    def get_existing_movie(self, tmdb_id: int) -> dict | None:
        """Return the Radarr movie record if it already exists in the library."""
        resp = self._get("/api/v3/movie", tmdbId=tmdb_id)
        movies = resp.json()
        return movies[0] if movies else None

    # ── Add movie ─────────────────────────────────────────────────────────────

    def get_or_create_tag(self, label: str) -> int:
        """Return the Radarr tag ID for label, creating the tag if it doesn't exist."""
        tags = self._get("/api/v3/tag").json()
        for t in tags:
            if t["label"].lower() == label.lower():
                log.debug(f"Found existing tag '{label}' (id={t['id']})")
                return t["id"]
        resp = self._post("/api/v3/tag", {"label": label})
        tag_id = resp.json()["id"]
        log.info(f"Created tag '{label}' (id={tag_id})")
        return tag_id

    def get_language_id(self, name: str) -> int:
        """Return the Radarr language ID for the given language name (e.g. 'Tamil')."""
        langs = self._get("/api/v3/language").json()
        for lang in langs:
            if lang["name"].lower() == name.lower():
                log.debug(f"Found language '{name}' (id={lang['id']})")
                return lang["id"]
        log.warning(f"Language '{name}' not found in Radarr; defaulting to id=1")
        return 1

    def add_movie(
        self,
        tmdb_result: dict,
        root_folder: str,
        quality_profile_id: int,
        language_profile_id: int,
        tags: list[int] | None = None,
    ) -> dict:
        """Add a movie to Radarr without triggering an automatic search."""
        payload = {
            "title": tmdb_result["title"],
            "year": tmdb_result.get("year", 0),
            "tmdbId": tmdb_result["tmdbId"],
            "qualityProfileId": quality_profile_id,
            "languageProfileId": language_profile_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "tags": tags or [],
            "addOptions": {
                "searchForMovie": False,
            },
        }
        log.info(f"Adding movie to Radarr: {payload['title']} ({payload['year']})")
        resp = self._post("/api/v3/movie", payload)
        movie = resp.json()
        log.info(f"Movie added. Radarr ID: {movie['id']}, folder: {movie.get('path', '?')}")
        return movie

    def update_movie_tags(self, movie: dict, tags: list[int]) -> None:
        """Merge tag IDs into an existing movie record and PUT the update."""
        existing_tags = movie.get("tags", [])
        merged = list(set(existing_tags) | set(tags))
        if merged == existing_tags:
            log.debug("Movie already has all required tags; no update needed.")
            return
        movie["tags"] = merged
        resp = self.session.put(
            f"{self.base}/api/v3/movie/{movie['id']}",
            json=movie,
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"Updated tags on movie id={movie['id']}: {merged}")

    # ── Manual import ─────────────────────────────────────────────────────────

    def manual_import_analyze(self, folder: str, movie_id: int = 0) -> list[dict]:
        """
        GET /api/v3/manualimport — ask Radarr to scan a folder and return candidates.
        folder must be the path AS SEEN BY THE RADARR CONTAINER.

        filterExistingFiles defaults to true in the API; we pass false so the file
        always appears even if Radarr thinks it already exists.
        """
        params: dict = {
            "folder": folder,
            "filterExistingFiles": False,   # boolean, not string
        }
        if movie_id:
            params["movieId"] = movie_id
        log.info(f"Analyzing folder for import (Radarr path): {folder}")
        try:
            resp = self._get("/api/v3/manualimport", **params)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 500 and movie_id:
                # Some Radarr versions return 500 when movieId is combined with
                # folder scan; retry without it and let the caller filter by filename.
                log.debug("Radarr returned 500 with movieId — retrying without it …")
                params.pop("movieId")
                resp = self._get("/api/v3/manualimport", **params)
            else:
                raise
        items = resp.json()
        log.info(f"Manual import analysis returned {len(items)} candidate(s)")
        for item in items:
            movie_match = (item.get("movie") or {}).get("title", "no match")
            quality_name = (item.get("quality") or {}).get("quality", {}).get("name", "?")
            rejections = [r.get("reason", "") for r in (item.get("rejections") or [])]
            log.info(
                f"  → {Path(item.get('path','?')).name}  "
                f"movie={movie_match}  quality={quality_name}"
                + (f"  rejections={rejections}" if rejections else "")
            )
        return items

    def manual_import_approve(
        self,
        items: list[dict],
        language_id: int = 1,
        language_name: str = "Tamil",
        release_group: str = "einthusan",
        movie_id: int = 0,
    ) -> None:
        """
        POST /api/v3/manualimport — submit import decisions to Radarr.

        Body is an array of ManualImportReprocessResource.  Key differences from
        what we had before (fixed per the OpenAPI spec):
          • include `id` from the GET response (Radarr uses it to find the file)
          • `movieId` is a direct int field, not nested
          • removed `shouldReplace` — not in the spec and rejected by
            `additionalProperties: false`

        movie_id: fallback Radarr movie ID used when Radarr's analyze step
          returns items with movie=null (auto-match failed).
        """
        if not items:
            log.warning("No items to import.")
            return
        payload = []
        for item in items:
            matched_movie_id = (item.get("movie") or {}).get("id") or movie_id
            if not matched_movie_id:
                log.warning(
                    f"Skipping '{Path(item['path']).name}': "
                    "Radarr could not match it to a movie and no fallback movie_id was provided."
                )
                continue
            if not item.get("movie"):
                log.info(
                    f"Radarr analyze returned movie=null for '{Path(item['path']).name}'; "
                    f"using fallback movie_id={matched_movie_id}"
                )
            payload.append({
                "id":           item["id"],
                "path":         item["path"],
                "movieId":      matched_movie_id,
                "quality":      item["quality"],
                "languages":    [{"id": language_id, "name": language_name}],
                "releaseGroup": release_group,
                "downloadId":   "",
            })
        if not payload:
            log.error(
                "Radarr could not match any file to a movie automatically.\n"
                "You may need to import manually via the Radarr UI."
            )
            return
        for entry in payload:
            log.info(
                f"  Importing: {Path(entry['path']).name}  "
                f"movieId={entry['movieId']}  quality={entry['quality'].get('quality',{}).get('name','?')}"
            )
        log.info(f"Submitting import for {len(payload)} file(s) …")
        self._post("/api/v3/manualimport", payload)
        log.info("Import submitted successfully.")

    def downloaded_movies_scan(self, file_path: str) -> None:
        """
        Fallback import: POST /api/v3/command DownloadedMoviesScan.

        Tells Radarr to treat a specific file as a completed download and
        import it automatically.  More permissive than manual import — Radarr
        handles path matching itself.
        """
        log.info(f"Triggering DownloadedMoviesScan for: {file_path}")
        self._post("/api/v3/command", {
            "name": "DownloadedMoviesScan",
            "path": file_path,
        })
        log.info("DownloadedMoviesScan command sent.")

    # ── Rescan ────────────────────────────────────────────────────────────────

    def rescan_movie(self, movie_id: int) -> int:
        """Trigger a disk rescan for a specific movie. Returns the Radarr command ID."""
        log.info(f"Triggering disk rescan for movie ID {movie_id} …")
        resp = self._post("/api/v3/command", {"name": "RescanMovie", "movieId": movie_id})
        return resp.json().get("id", 0)

    def wait_for_command(self, command_id: int, timeout: int = 60) -> bool:
        """
        Poll GET /api/v3/command/{id} until Radarr reports the command as
        completed, failed, or aborted — or until timeout seconds have elapsed.
        Returns True on success, False on failure/timeout.
        """
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                data = self._get(f"/api/v3/command/{command_id}").json()
                status = data.get("status", "")
                if status == "completed":
                    log.info(f"Radarr command {command_id} ({data.get('name','')}) completed ✓")
                    return True
                if status in ("failed", "aborted"):
                    log.warning(
                        f"Radarr command {command_id} ({data.get('name','')}) "
                        f"ended with status={status}: {data.get('message','')}"
                    )
                    return False
            except Exception:
                pass
            _time.sleep(1)
        log.warning(f"Timed out waiting for Radarr command {command_id}")
        return False

    # ── Get quality profiles ──────────────────────────────────────────────────

    def list_quality_profiles(self) -> list[dict]:
        return self._get("/api/v3/qualityprofile").json()

    def list_root_folders(self) -> list[dict]:
        return self._get("/api/v3/rootfolder").json()


# ── Interactive movie selection ───────────────────────────────────────────────

def pick_movie_from_results(results: list[dict], title: str, year: int) -> dict:
    """
    Given TMDB results, either auto-select an exact match or prompt the user.
    """
    if not results:
        log.error(f"No TMDB results found for '{title}' ({year}). Cannot add to Radarr.")
        sys.exit(1)

    # Try exact match first
    for r in results:
        if r.get("year") == year and r.get("title", "").lower() == title.lower():
            log.info(f"Auto-matched: {r['title']} ({r['year']}) [tmdbId={r['tmdbId']}]")
            return r

    # Show options
    print(f"\nFound {len(results)} result(s) for '{title}' ({year or '?'}):\n")
    for i, r in enumerate(results[:10], 1):
        print(f"  [{i}] {r.get('title','?')} ({r.get('year','?')})  tmdbId={r.get('tmdbId','?')}")
        if r.get("overview"):
            print(f"      {r['overview'][:100]}…")

    while True:
        choice = input("\nEnter number to select, or 0 to abort: ").strip()
        if choice == "0":
            log.info("Aborted by user.")
            sys.exit(0)
        if choice.isdigit() and 1 <= int(choice) <= min(len(results), 10):
            return results[int(choice) - 1]
        print("Invalid choice, try again.")


# ── File naming ───────────────────────────────────────────────────────────────

def safe_filename(title: str, year: int, ext: str = ".mp4") -> str:
    """Return a Radarr-compatible filename: 'Title (Year).mp4'"""
    safe = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    return f"{safe} ({year}){ext}" if year else f"{safe}{ext}"


def detect_extension(url: str) -> str:
    """Guess file extension from the video URL."""
    path = urllib.parse.urlparse(url).path
    for ext in (".mp4", ".mkv", ".avi", ".ts"):
        if path.lower().endswith(ext):
            return ext
    return ".mp4"


# ── Main orchestration ────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    cfg = load_config()

    # ── Radarr connectivity check ─────────────────────────────────────────────
    radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
    if not radarr.ping():
        sys.exit(1)
    log.info(f"Radarr connected at {cfg['radarr']['url']}")

    # ── Radarr-only mode: skip einthusan, import existing file ────────────────
    if args.radarr_only:
        file_path = Path(args.radarr_only)
        if not file_path.exists():
            log.error(f"File not found: {file_path}")
            sys.exit(1)
        _radarr_import(radarr, cfg, file_path, args)
        return

    # ── Einthusan: login + extract movie info ─────────────────────────────────
    client = EinthusanClient(
        cfg["einthusan"]["username"],
        cfg["einthusan"]["password"],
        cfg["einthusan"]["cookies"],
    )
    movie_info = client.get_movie_info(args.url)

    title = movie_info["title"]
    year = movie_info["year"]
    video_url = movie_info["video_url"]
    ext = detect_extension(video_url)
    filename = safe_filename(title, year, ext)

    log.info(f"Movie: {title} ({year})")
    log.info(f"Video URL: {video_url}")

    # ── Download-only mode ────────────────────────────────────────────────────
    staging = Path(cfg["staging_host"])
    dest_path = staging / filename

    if not args.skip_download:
        client.download(video_url, dest_path)
    else:
        log.info("Skipping download (--skip-download set).")

    if args.download_only:
        log.info(f"Download-only mode. File at: {dest_path}")
        return

    # ── Radarr: find or add movie ─────────────────────────────────────────────
    _radarr_import(radarr, cfg, dest_path, args, title=title, year=year)


def _radarr_import(
    radarr: RadarrClient,
    cfg: dict,
    file_path: Path,
    args: argparse.Namespace,
    title: str = "",
    year: int = 0,
) -> None:
    """Handle the Radarr lookup → add → import pipeline."""

    # Derive title/year from filename if not supplied
    if not title:
        m = re.match(r"^(.+?)\s*\((\d{4})\)", file_path.stem)
        if m:
            title, year = m.group(1).strip(), int(m.group(2))
        else:
            title = file_path.stem
            year = 0

    # Resolve the "einthusan" tag ID and Tamil language ID up front
    einthusan_tag_id = radarr.get_or_create_tag("einthusan")
    tamil_language_id = radarr.get_language_id("Tamil")

    # Search TMDB
    results = radarr.lookup_movie(title, year)
    chosen = pick_movie_from_results(results, title, year)

    tmdb_id = chosen["tmdbId"]
    movie_title = chosen["title"]
    movie_year = chosen.get("year", year)

    # Check if already in Radarr
    existing = radarr.get_existing_movie(tmdb_id)
    if existing:
        log.info(f"Movie already in Radarr (id={existing['id']}): {movie_title}")
        radarr_movie = existing
        radarr.update_movie_tags(radarr_movie, [einthusan_tag_id])
    else:
        radarr_movie = radarr.add_movie(
            chosen,
            cfg["radarr"]["root_folder"],
            cfg["radarr"]["quality_profile_id"],
            cfg["radarr"]["language_profile_id"],
            tags=[einthusan_tag_id],
        )

    movie_id = radarr_movie["id"]

    # Rename the staged file to match Radarr's expected naming
    ext = file_path.suffix
    ideal_name = safe_filename(movie_title, movie_year, ext)
    ideal_path = file_path.parent / ideal_name
    if file_path != ideal_path and file_path.exists():
        log.info(f"Renaming: {file_path.name} → {ideal_name}")
        file_path.rename(ideal_path)
        file_path = ideal_path

    # Trigger manual import analysis
    # The folder path must be as seen by the Radarr container
    staging = cfg["staging_host"]
    import_items = radarr.manual_import_analyze(staging, movie_id)

    # Filter to only the file we just downloaded
    matching = [
        item for item in import_items
        if Path(item["path"]).name == file_path.name
    ]

    if not matching:
        log.warning(
            f"Radarr did not find '{file_path.name}' in its import analysis.\n"
            f"Checked path: {staging}\n"
            "Possible causes:\n"
            "  • File permissions prevent Radarr from reading the file\n"
            "  • Try importing manually in the Radarr UI: Movies → Manual Import"
        )
        log.info(f"Files seen by Radarr analysis: {[i['path'] for i in import_items]}")
        return

    radarr.manual_import_approve(
        matching,
        language_id=tamil_language_id,
        language_name="Tamil",
        release_group="einthusan",
        movie_id=movie_id,
    )

    # Final rescan to confirm
    time.sleep(3)
    radarr.rescan_movie(movie_id)
    log.info(
        f"\n✓ Done! '{movie_title} ({movie_year})' should now appear in Jellyfin.\n"
        f"  Radarr ID : {movie_id}\n"
        f"  TMDB ID   : {tmdb_id}\n"
        f"  File      : {file_path}"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download a Tamil movie from Einthusan.tv and import it into Radarr/Jellyfin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", nargs="?", help="Einthusan movie URL")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download the file; skip Radarr import",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading; only run the Radarr import step",
    )
    parser.add_argument(
        "--radarr-only",
        metavar="FILE",
        help="Skip Einthusan entirely; import an existing local file into Radarr",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Print Radarr quality profiles and root folders, then exit",
    )

    args = parser.parse_args()
    setup_logging(args.debug)

    # Load config early so --list-profiles works without a URL
    if args.list_profiles:
        cfg = load_config()
        radarr = RadarrClient(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
        print("\n── Quality Profiles ──")
        for p in radarr.list_quality_profiles():
            print(f"  id={p['id']}  name={p['name']}")
        print("\n── Root Folders ──")
        for f in radarr.list_root_folders():
            print(f"  id={f['id']}  path={f['path']}  free={f.get('freeSpace',0)//1e9:.0f} GB")
        return

    if not args.url and not args.radarr_only:
        parser.error("Provide an Einthusan movie URL, or use --radarr-only <file>")

    run(args)


if __name__ == "__main__":
    main()
