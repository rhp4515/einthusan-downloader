import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.jobs import Job, JobStore, Progress


class TestJobStore:
    def test_create_returns_job_with_uuid_id_and_resolving_state(self):
        store = JobStore()

        job = store.create("https://einthusan.tv/movie/watch/abc123/")

        assert job.einthusan_url == "https://einthusan.tv/movie/watch/abc123/"
        assert job.state == "resolving"
        assert len(job.id) == 36  # uuid4 string

    def test_get_returns_none_for_unknown_id(self):
        store = JobStore()
        assert store.get("nonexistent") is None

    def test_get_returns_created_job(self):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc123/")

        assert store.get(job.id) is job

    def test_list_returns_all_jobs(self):
        store = JobStore()
        j1 = store.create("https://einthusan.tv/movie/watch/a/")
        j2 = store.create("https://einthusan.tv/movie/watch/b/")

        assert {j.id for j in store.list()} == {j1.id, j2.id}

    def test_update_sets_fields_and_bumps_updated_at(self):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc123/")
        original_updated_at = job.updated_at

        updated = store.update(job.id, state="awaiting_verification", selected_tmdb_id=111)

        assert updated.state == "awaiting_verification"
        assert updated.selected_tmdb_id == 111
        assert updated.updated_at >= original_updated_at

    def test_update_returns_none_for_unknown_id(self):
        store = JobStore()
        assert store.update("nonexistent", state="done") is None

    def test_delete_removes_and_returns_job(self):
        store = JobStore()
        job = store.create("https://einthusan.tv/movie/watch/abc123/")

        deleted = store.delete(job.id)

        assert deleted.id == job.id
        assert store.get(job.id) is None

    def test_delete_returns_none_for_unknown_id(self):
        store = JobStore()
        assert store.delete("nonexistent") is None

    def test_concurrent_creates_are_thread_safe(self):
        store = JobStore()
        ids: list[str] = []
        lock = threading.Lock()

        def worker():
            job = store.create("https://einthusan.tv/movie/watch/abc123/")
            with lock:
                ids.append(job.id)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(ids)) == 20
        assert len(store.list()) == 20


class TestProgressDefaults:
    def test_defaults_to_zero(self):
        p = Progress()
        assert p.downloaded_bytes == 0
        assert p.total_bytes == 0
        assert p.eta_seconds is None
