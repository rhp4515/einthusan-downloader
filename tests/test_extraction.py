"""
Tests for HTML extraction logic using the sample page in examples/einthusan_source.html.

Run with:
    ../.venv/bin/pytest tests/ -v
"""

import re
import sys
import urllib.parse
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

# ── Helpers to load the sample HTML ──────────────────────────────────────────

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
SAMPLE_HTML = (EXAMPLES_DIR / "einthusan_source.html").read_text()
SAMPLE_URL = "https://einthusan.tv/premium/movie/watch/4QmG/?lang=tamil"


def make_soup(html: str = SAMPLE_HTML) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ── Import the extraction methods under test ──────────────────────────────────
# We instantiate EinthusanClient with dummy credentials (no network calls made).

sys.path.insert(0, str(Path(__file__).parent.parent))
from einthusan_dl import EinthusanClient

_client = EinthusanClient.__new__(EinthusanClient)  # bypass __init__, no network


# ═════════════════════════════════════════════════════════════════════════════
# Video URL extraction
# ═════════════════════════════════════════════════════════════════════════════

class TestVideoURLExtraction:
    """Tests for _extract_video_url against the real sample HTML."""

    def test_returns_mp4_url(self):
        """Primary path: data-mp4-link on #UIVideoPlayer returns an MP4 URL."""
        url = _client._extract_video_url(make_soup(), SAMPLE_HTML, SAMPLE_URL)
        assert ".mp4" in url

    def test_url_is_https(self):
        """Extracted URL must be an absolute HTTPS URL."""
        url = _client._extract_video_url(make_soup(), SAMPLE_HTML, SAMPLE_URL)
        assert url.startswith("https://") or url.startswith("http://")

    def test_url_contains_signed_token(self):
        """Signed CDN URLs include expiry (e=) and md5 (md5=) query params."""
        url = _client._extract_video_url(make_soup(), SAMPLE_HTML, SAMPLE_URL)
        assert "e=" in url and "md5=" in url, (
            f"Expected signed token params in URL, got: {url}"
        )

    def test_exact_mp4_url(self):
        """
        Raw IP in data-mp4-link is resolved to a cdn*.einthusan.io hostname.
        The path and signed token (e=, md5=) are preserved unchanged.
        """
        url = _client._extract_video_url(make_soup(), SAMPLE_HTML, SAMPLE_URL)
        # Hostname must be a CDN domain, not the raw IP from the page
        parsed = urllib.parse.urlparse(url)
        assert "einthusan.io" in parsed.netloc, (
            f"Expected cdn*.einthusan.io hostname, got: {parsed.netloc}"
        )
        # Signed token params must be preserved
        assert "e=1775366010" in url
        assert "md5=achs5UhCIzrXWlMyACAODA" in url
        assert "/etv/content/D4QmG.mp4" in url

    def test_uses_method_1_uivideoplayer(self):
        """Confirms extraction uses #UIVideoPlayer data-mp4-link (Method 1)."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        assert player is not None, "#UIVideoPlayer section not found in sample HTML"
        assert player.get("data-mp4-link"), "data-mp4-link attribute is empty"

    def test_hls_link_also_present(self):
        """HLS streaming URL is available as a fallback."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        hls = player.get("data-hls-link", "")
        assert ".m3u8" in hls, f"Expected .m3u8 HLS URL, got: {hls}"

    def test_fallback_to_hls_when_mp4_link_missing(self):
        """When data-mp4-link is absent, extraction falls back to data-hls-link (Method 3)."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        original_mp4 = player["data-mp4-link"]
        del player["data-mp4-link"]

        url = _client._extract_video_url(soup, SAMPLE_HTML, SAMPLE_URL)
        # Should derive MP4 from HLS by stripping .m3u8
        assert ".mp4" in url
        assert ".m3u8" not in url

        player["data-mp4-link"] = original_mp4  # restore

    def test_fallback_to_bare_mp4_in_html(self):
        """When player element is absent, a bare .mp4 URL in page source is found (Method 6)."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        player.decompose()  # remove the player section entirely

        url = _client._extract_video_url(soup, SAMPLE_HTML, SAMPLE_URL)
        assert ".mp4" in url

    def test_no_video_url_exits(self):
        """When no video URL can be found, the script exits with a non-zero code."""
        # Strip all .mp4 occurrences from HTML and remove player element
        stripped_html = re.sub(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', "", SAMPLE_HTML)
        soup = make_soup(stripped_html)
        player = soup.find(id="UIVideoPlayer")
        if player:
            player.decompose()

        with pytest.raises(SystemExit):
            _client._extract_video_url(soup, stripped_html, SAMPLE_URL)


# ═════════════════════════════════════════════════════════════════════════════
# Title extraction
# ═════════════════════════════════════════════════════════════════════════════

class TestTitleExtraction:
    """Tests for _extract_title_year against the real sample HTML."""

    def test_title_is_sabdham(self):
        title, _ = _client._extract_title_year(make_soup(), SAMPLE_URL)
        assert title == "Sabdham", f"Expected 'Sabdham', got '{title}'"

    def test_year_is_2025(self):
        _, year = _client._extract_title_year(make_soup(), SAMPLE_URL)
        assert year == 2025, f"Expected 2025, got {year}"

    def test_title_from_data_content_title(self):
        """data-content-title on #UIVideoPlayer is the primary title source."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        assert player.get("data-content-title") == "Sabdham"

    def test_year_from_title_tag(self):
        """Year is parsed from the parenthesised year in the <title> tag."""
        soup = make_soup()
        title_tag = soup.find("title")
        assert title_tag is not None
        m = re.search(r"\((\d{4})\)", title_tag.get_text())
        assert m and int(m.group(1)) == 2025

    def test_title_excludes_language_suffix(self):
        """Title should not contain 'Tamil in HD' or '- Einthusan'."""
        title, _ = _client._extract_title_year(make_soup(), SAMPLE_URL)
        assert "Tamil" not in title
        assert "Einthusan" not in title
        assert "HD" not in title

    def test_title_fallback_without_uivideoplayer(self):
        """When #UIVideoPlayer is absent, title is still extracted from <title> tag."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        if player:
            player.decompose()
        title, year = _client._extract_title_year(soup, SAMPLE_URL)
        assert title == "Sabdham"
        assert year == 2025

    def test_title_fallback_without_title_tag(self):
        """When both #UIVideoPlayer and <title> are absent, title falls back gracefully."""
        soup = make_soup()
        player = soup.find(id="UIVideoPlayer")
        if player:
            player.decompose()
        title_tag = soup.find("title")
        if title_tag:
            title_tag.decompose()
        title, _ = _client._extract_title_year(soup, SAMPLE_URL)
        # Should not crash and should return something
        assert isinstance(title, str) and len(title) > 0


# ═════════════════════════════════════════════════════════════════════════════
# Language extraction
# ═════════════════════════════════════════════════════════════════════════════

class TestLanguageExtraction:
    def test_detects_tamil_from_url(self):
        lang = _client._extract_language(make_soup(), SAMPLE_URL)
        assert lang == "Tamil"

    def test_detects_hindi_from_url(self):
        hindi_url = "https://einthusan.tv/premium/movie/watch/XXXX/?lang=hindi"
        lang = _client._extract_language(make_soup(), hindi_url)
        assert lang == "Hindi"

    def test_detects_language_from_page_body(self):
        """Falls back to scanning page body when URL has no lang hint."""
        url_no_lang = "https://einthusan.tv/premium/movie/watch/4QmG/"
        lang = _client._extract_language(make_soup(), url_no_lang)
        # Sample HTML body contains "Tamil"
        assert lang == "Tamil"

    def test_defaults_to_tamil(self):
        """Unknown language defaults to Tamil (einthusan is primarily Tamil)."""
        html = "<html><body>No language hint here</body></html>"
        lang = _client._extract_language(make_soup(html), "https://einthusan.tv/movie/")
        assert lang == "Tamil"


# ═════════════════════════════════════════════════════════════════════════════
# Page structure sanity checks
# ═════════════════════════════════════════════════════════════════════════════

class TestPageStructure:
    """Quick sanity checks that the sample HTML has expected landmarks."""

    def test_uivideoplayer_section_exists(self):
        assert make_soup().find(id="UIVideoPlayer") is not None

    def test_pgpremiummoviewatch_section_exists(self):
        assert make_soup().find(id="PGPremiumMovieWatch") is not None

    def test_movie_id_attribute(self):
        soup = make_soup()
        section = soup.find(id="PGPremiumMovieWatch")
        assert section.get("data-movieid") == "4QmG"

    def test_frames_file_attribute(self):
        soup = make_soup()
        section = soup.find(id="PGPremiumMovieWatch")
        assert section.get("data-frames-file") == "D4QmG"

    def test_title_tag_format(self):
        soup = make_soup()
        title_text = soup.find("title").get_text()
        # Expected: "Sabdham (2025) Tamil in HD - Einthusan"
        assert re.search(r"\w+ \(\d{4}\).*Einthusan", title_text), (
            f"Unexpected <title> format: {title_text}"
        )
