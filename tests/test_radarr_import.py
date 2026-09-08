"""
Tests for Radarr import metadata: Tamil language, 'einthusan' release group, 'einthusan' tag.

These tests mock the Radarr HTTP calls so no live Radarr instance is required.

Run with:
    ../.venv/bin/pytest tests/ -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from einthusan_dl import RadarrClient

# ── Shared fixtures ───────────────────────────────────────────────────────────

RADARR_LANGUAGES = [
    {"id": 1,  "name": "English"},
    {"id": 11, "name": "Tamil"},
    {"id": 3,  "name": "Hindi"},
]

RADARR_TAGS_EMPTY: list = []
RADARR_TAGS_WITH_EINTHUSAN = [{"id": 5, "label": "einthusan"}]

SAMPLE_IMPORT_ITEM = {
    "id": 99,   # required by ManualImportReprocessResource spec
    "path": "/data/media/manual_imports/Sabdham (2025).mp4",
    "movie": {"id": 42},
    "quality": {"quality": {"id": 1, "name": "Unknown"}, "revision": {"version": 1}},
    "languages": [{"id": 1, "name": "English"}],
    "releaseGroup": "",
}


def make_client() -> RadarrClient:
    return RadarrClient("http://localhost:7878", "testapikey")


# ─────────────────────────────────────────────────────────────────────────────
# get_language_id
# ─────────────────────────────────────────────────────────────────────────────

class TestGetLanguageId:
    def _mock_get(self, client, data):
        resp = MagicMock()
        resp.json.return_value = data
        client._get = MagicMock(return_value=resp)

    def test_returns_tamil_id(self):
        client = make_client()
        self._mock_get(client, RADARR_LANGUAGES)
        assert client.get_language_id("Tamil") == 11

    def test_case_insensitive(self):
        client = make_client()
        self._mock_get(client, RADARR_LANGUAGES)
        assert client.get_language_id("tamil") == 11

    def test_unknown_language_defaults_to_1(self):
        client = make_client()
        self._mock_get(client, RADARR_LANGUAGES)
        assert client.get_language_id("Klingon") == 1

    def test_queries_language_endpoint(self):
        client = make_client()
        self._mock_get(client, RADARR_LANGUAGES)
        client.get_language_id("Tamil")
        client._get.assert_called_once_with("/api/v3/language")


# ─────────────────────────────────────────────────────────────────────────────
# get_or_create_tag
# ─────────────────────────────────────────────────────────────────────────────

class TestGetOrCreateTag:
    def test_returns_existing_tag_id(self):
        client = make_client()
        get_resp = MagicMock()
        get_resp.json.return_value = RADARR_TAGS_WITH_EINTHUSAN
        client._get = MagicMock(return_value=get_resp)

        tag_id = client.get_or_create_tag("einthusan")
        assert tag_id == 5
        client._get.assert_called_once_with("/api/v3/tag")

    def test_creates_tag_when_absent(self):
        client = make_client()
        get_resp = MagicMock()
        get_resp.json.return_value = RADARR_TAGS_EMPTY
        client._get = MagicMock(return_value=get_resp)

        post_resp = MagicMock()
        post_resp.json.return_value = {"id": 7, "label": "einthusan"}
        client._post = MagicMock(return_value=post_resp)

        tag_id = client.get_or_create_tag("einthusan")
        assert tag_id == 7
        client._post.assert_called_once_with("/api/v3/tag", {"label": "einthusan"})

    def test_tag_lookup_is_case_insensitive(self):
        client = make_client()
        get_resp = MagicMock()
        get_resp.json.return_value = [{"id": 5, "label": "Einthusan"}]
        client._get = MagicMock(return_value=get_resp)

        tag_id = client.get_or_create_tag("einthusan")
        assert tag_id == 5  # found, no POST needed


# ─────────────────────────────────────────────────────────────────────────────
# add_movie — tags
# ─────────────────────────────────────────────────────────────────────────────

class TestAddMovieTags:
    def _make_client_with_post(self, returned_movie: dict) -> tuple[RadarrClient, MagicMock]:
        client = make_client()
        post_resp = MagicMock()
        post_resp.json.return_value = returned_movie
        client._post = MagicMock(return_value=post_resp)
        return client, client._post

    def test_add_movie_includes_einthusan_tag(self):
        returned_movie = {"id": 42, "path": "/data/media/movies/Sabdham (2025)"}
        client, mock_post = self._make_client_with_post(returned_movie)

        client.add_movie(
            tmdb_result={"title": "Sabdham", "year": 2025, "tmdbId": 12345},
            root_folder="/data/media/movies",
            quality_profile_id=1,
            language_profile_id=1,
            tags=[5],
        )

        payload = mock_post.call_args[0][1]
        assert payload["tags"] == [5]

    def test_add_movie_defaults_to_empty_tags(self):
        returned_movie = {"id": 42, "path": "/data/media/movies/Sabdham (2025)"}
        client, mock_post = self._make_client_with_post(returned_movie)

        client.add_movie(
            tmdb_result={"title": "Sabdham", "year": 2025, "tmdbId": 12345},
            root_folder="/data/media/movies",
            quality_profile_id=1,
            language_profile_id=1,
        )

        payload = mock_post.call_args[0][1]
        assert payload["tags"] == []


# ─────────────────────────────────────────────────────────────────────────────
# update_movie_tags — existing movie
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateMovieTags:
    def _mock_put(self, client) -> MagicMock:
        put_resp = MagicMock()
        put_resp.raise_for_status = MagicMock()
        mock = MagicMock(return_value=put_resp)
        client.session = MagicMock()
        client.session.put = mock
        return mock

    def test_adds_missing_tag_to_existing_movie(self):
        client = make_client()
        mock_put = self._mock_put(client)
        movie = {"id": 42, "tags": []}

        client.update_movie_tags(movie, [5])

        mock_put.assert_called_once()
        sent_payload = mock_put.call_args[1]["json"]
        assert 5 in sent_payload["tags"]

    def test_does_not_duplicate_existing_tag(self):
        client = make_client()
        mock_put = self._mock_put(client)
        movie = {"id": 42, "tags": [5]}

        client.update_movie_tags(movie, [5])

        # Already has the tag — PUT should NOT be called
        mock_put.assert_not_called()

    def test_merges_new_tag_with_existing_tags(self):
        client = make_client()
        mock_put = self._mock_put(client)
        movie = {"id": 42, "tags": [3]}

        client.update_movie_tags(movie, [5])

        sent_payload = mock_put.call_args[1]["json"]
        assert set(sent_payload["tags"]) == {3, 5}


# ─────────────────────────────────────────────────────────────────────────────
# manual_import_approve — language and release group
# ─────────────────────────────────────────────────────────────────────────────

class TestManualImportApprove:
    def _mock_post(self, client) -> MagicMock:
        post_resp = MagicMock()
        post_resp.json.return_value = {}
        mock = MagicMock(return_value=post_resp)
        client._post = mock
        return mock

    def test_language_is_tamil(self):
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve(
            [SAMPLE_IMPORT_ITEM],
            language_id=11,
            language_name="Tamil",
            release_group="einthusan",
        )

        payload = mock_post.call_args[0][1]["files"]
        assert payload[0]["languages"] == [{"id": 11, "name": "Tamil"}]

    def test_release_group_is_einthusan(self):
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve(
            [SAMPLE_IMPORT_ITEM],
            language_id=11,
            language_name="Tamil",
            release_group="einthusan",
        )

        payload = mock_post.call_args[0][1]["files"]
        assert payload[0]["releaseGroup"] == "einthusan"

    def test_language_not_overridden_by_item_default(self):
        """Radarr's analysis may return English as default — we must override it."""
        client = make_client()
        mock_post = self._mock_post(client)

        item_with_english = {**SAMPLE_IMPORT_ITEM, "languages": [{"id": 1, "name": "English"}]}
        client.manual_import_approve(
            [item_with_english],
            language_id=11,
            language_name="Tamil",
            release_group="einthusan",
        )

        payload = mock_post.call_args[0][1]["files"]
        assert payload[0]["languages"] == [{"id": 11, "name": "Tamil"}]

    def test_default_values_are_tamil_and_einthusan(self):
        """Calling without explicit args still uses Tamil + einthusan defaults."""
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve([SAMPLE_IMPORT_ITEM])

        payload = mock_post.call_args[0][1]["files"]
        assert payload[0]["languages"] == [{"id": 1, "name": "Tamil"}]
        assert payload[0]["releaseGroup"] == "einthusan"

    def test_skips_items_without_movie_match(self):
        """Items where Radarr couldn't match a movie are excluded from the payload."""
        client = make_client()
        mock_post = self._mock_post(client)
        unmatched = {**SAMPLE_IMPORT_ITEM, "movie": None}

        client.manual_import_approve([unmatched])

        mock_post.assert_not_called()

    def test_queues_a_manualimport_command(self):
        """The import must be queued as a command — POST /api/v3/manualimport is
        only the reprocess step and imports nothing."""
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve([SAMPLE_IMPORT_ITEM])

        mock_post.assert_called_once()
        endpoint, body = mock_post.call_args[0][0], mock_post.call_args[0][1]
        assert endpoint == "/api/v3/command"
        assert body["name"] == "ManualImport"
        assert body["importMode"] == "move"
        assert body["files"][0]["movieId"] == 42

    def test_returns_command_id(self):
        client = make_client()
        post_resp = MagicMock()
        post_resp.json.return_value = {"id": 4242}
        client._post = MagicMock(return_value=post_resp)

        assert client.manual_import_approve([SAMPLE_IMPORT_ITEM]) == 4242

    def test_returns_zero_when_nothing_submitted(self):
        client = make_client()
        self._mock_post(client)

        assert client.manual_import_approve([]) == 0

    def test_payload_includes_id_from_get_response(self):
        """POST payload must include 'id' from the GET analysis response (per OpenAPI spec)."""
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve([SAMPLE_IMPORT_ITEM])

        payload = mock_post.call_args[0][1]["files"]
        assert "id" in payload[0], "POST payload must include 'id' field"
        assert payload[0]["id"] == 99

    def test_payload_excludes_should_replace(self):
        """'shouldReplace' must NOT be in the payload — the spec uses additionalProperties:false."""
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve([SAMPLE_IMPORT_ITEM])

        payload = mock_post.call_args[0][1]["files"]
        assert "shouldReplace" not in payload[0], (
            "'shouldReplace' is not in ManualImportReprocessResource and will cause a 400/500"
        )

    def test_movie_id_is_direct_int_field(self):
        """movieId must be a direct int, not nested under 'movie'."""
        client = make_client()
        mock_post = self._mock_post(client)

        client.manual_import_approve([SAMPLE_IMPORT_ITEM])

        payload = mock_post.call_args[0][1]["files"]
        assert payload[0]["movieId"] == 42
        assert isinstance(payload[0]["movieId"], int)


# ─────────────────────────────────────────────────────────────────────────────
# add_movie — monitored parameter
# ─────────────────────────────────────────────────────────────────────────────

class TestAddMovieMonitored:
    def _make_client_with_post(self, returned_movie: dict) -> tuple[RadarrClient, MagicMock]:
        client = make_client()
        post_resp = MagicMock()
        post_resp.json.return_value = returned_movie
        client._post = MagicMock(return_value=post_resp)
        return client, client._post

    def test_add_movie_defaults_to_monitored_true(self):
        returned_movie = {"id": 42, "path": "/data/media/movies/Sabdham (2025)"}
        client, mock_post = self._make_client_with_post(returned_movie)

        client.add_movie(
            tmdb_result={"title": "Sabdham", "year": 2025, "tmdbId": 12345},
            root_folder="/data/media/movies",
            quality_profile_id=1,
            language_profile_id=1,
        )

        payload = mock_post.call_args[0][1]
        assert payload["monitored"] is True

    def test_add_movie_unmonitored_when_requested(self):
        returned_movie = {"id": 42, "path": "/data/media/movies/Sabdham (2025)"}
        client, mock_post = self._make_client_with_post(returned_movie)

        client.add_movie(
            tmdb_result={"title": "Sabdham", "year": 2025, "tmdbId": 12345},
            root_folder="/data/media/movies",
            quality_profile_id=1,
            language_profile_id=1,
            monitored=False,
        )

        payload = mock_post.call_args[0][1]
        assert payload["monitored"] is False


# ─────────────────────────────────────────────────────────────────────────────
# update_movie — full movie record update
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateMovie:
    def test_puts_full_movie_record_and_returns_response(self):
        client = make_client()
        put_resp = MagicMock()
        put_resp.raise_for_status = MagicMock()
        put_resp.json.return_value = {"id": 42, "monitored": True}
        client.session = MagicMock()
        client.session.put = MagicMock(return_value=put_resp)

        movie = {"id": 42, "monitored": True, "title": "Sabdham"}
        result = client.update_movie(movie)

        client.session.put.assert_called_once_with(
            "http://localhost:7878/api/v3/movie/42",
            json=movie,
            timeout=30,
        )
        assert result == {"id": 42, "monitored": True}


# ─────────────────────────────────────────────────────────────────────────────
# delete_movie — remove from library
# ─────────────────────────────────────────────────────────────────────────────

class TestDeleteMovie:
    def test_deletes_without_files_by_default(self):
        client = make_client()
        del_resp = MagicMock()
        del_resp.raise_for_status = MagicMock()
        client.session = MagicMock()
        client.session.delete = MagicMock(return_value=del_resp)

        client.delete_movie(42)

        client.session.delete.assert_called_once_with(
            "http://localhost:7878/api/v3/movie/42",
            params={"deleteFiles": "false"},
            timeout=30,
        )

    def test_deletes_with_files_when_requested(self):
        client = make_client()
        del_resp = MagicMock()
        del_resp.raise_for_status = MagicMock()
        client.session = MagicMock()
        client.session.delete = MagicMock(return_value=del_resp)

        client.delete_movie(42, delete_files=True)

        sent_params = client.session.delete.call_args[1]["params"]
        assert sent_params == {"deleteFiles": "true"}
