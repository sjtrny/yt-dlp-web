"""Playlist discovery, shared ownership, and bounded admission contracts."""

import os
import subprocess
import sys
import time
from unittest.mock import patch

import app as web
from test_api_tasks import DownloadTestCase, ROOT


class PlaylistTests(DownloadTestCase):
    limit = 2

    def job(self, job):
        return self.client.get(job["status_url"]).get_json()["job"]

    def video(self, number):
        return self.submit(f"https://example.test/video-entry-{number}").get_json()["job"]

    def release(self, number):
        (self.directory / f"release-video-entry-{number}").touch()

    def playlist(self, suffix=""):
        job = self.submit("https://example.test/playlist" + suffix).get_json()["job"]
        self.wait_for(lambda: self.job(job)["kind"] == "playlist")
        return job

    def test_limit_queue_cancellation_and_fifo(self):
        videos = [self.video(number) for number in range(1, 3)]
        self.wait_for(lambda: all(self.job(job)["status"] == "downloading" for job in videos))
        videos.extend(self.video(number) for number in range(3, 6))
        self.wait_for(lambda: all(web.find_job(job["id"])["resolved"] for job in videos))
        self.assertEqual([self.job(job)["status"] for job in videos[2:]], ["queued"] * 3)
        self.assertEqual(self.client.get("/api/v1/downloads?status=queued").status_code, 200)
        duplicate = self.submit(videos[3]["url"])
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.get_json()["job"]["id"], videos[3]["id"])
        # The database also enforces ownership while a job waits for a slot.
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            web.store.save_job(dict(web.find_job(videos[3]["id"]), id="duplicate-queued"))
        response = self.client.post(videos[2]["status_url"] + "/stop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job"]["status"], "stopped")
        self.assertFalse((self.directory / "started-video-entry-3").exists())
        self.release(1)
        self.wait_for(lambda: self.job(videos[3])["status"] == "downloading")
        self.assertEqual(self.job(videos[4])["status"], "queued")
        self.release(2)
        self.wait_for(lambda: self.job(videos[4])["status"] == "downloading")
        self.assertFalse((self.directory / "started-video-entry-3").exists())

    def test_incremental_discovery_individual_files_and_unavailable_entries(self):
        playlist = self.playlist("-slow-mixed")
        self.wait_for(lambda: self.job(playlist)["counts"]["active"] == 2)
        self.assertFalse(self.job(playlist)["discovery_done"])
        self.assertEqual(self.job(playlist)["counts"]["total"], 2)
        self.release(1)
        self.wait_for(lambda: self.job(playlist)["counts"]["complete"] == 1)
        first = self.job(playlist)["entries"][0]["id"]
        with self.client.get(f"/download/{first}") as response:
            self.assertEqual(response.status_code, 200)
        self.assertIn(f"/download/{first}", self.client.get("/status").get_data(as_text=True))
        (self.directory / "release-discovery").touch()
        self.wait_for(lambda: self.job(playlist)["discovery_done"])
        counts = self.job(playlist)["counts"]
        self.assertEqual(counts, dict(total=7, complete=1, active=2, queued=3, stopped=0, failed=1))
        self.assertIsNone(self.job(playlist)["download_url"])
        self.assertEqual(self.client.get(playlist["status_url"] + "/file").status_code, 404)
        self.client.post(f"/completed/{first}/remove")
        self.assertEqual(self.job(playlist)["counts"]["complete"], 1)
        for number in range(2, 7):
            self.release(number)
        self.wait_for(lambda: self.job(playlist)["status"] == "failed")
        self.assertEqual(self.job(playlist)["counts"]["complete"], 6)
        for entry in self.job(playlist)["entries"][:6]:
            with self.client.get(f'/api/v1/downloads/{entry["id"]}/file') as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(self.directory.glob("*.mp4"))), 6)

    def test_stop_playlist_retains_partial_files_and_does_not_start_queued_videos(self):
        playlist = self.playlist()
        self.wait_for(lambda: self.job(playlist)["discovery_done"])
        self.wait_for(lambda: len(list(self.directory.glob("*.part"))) == 2)
        self.release(1)
        self.wait_for(lambda: self.job(playlist)["counts"]["complete"] == 1)
        self.wait_for(lambda: (self.directory / "started-video-entry-3").exists())
        response = self.client.post(playlist["status_url"] + "/stop")
        self.assertEqual(response.status_code, 202)
        self.wait_for(lambda: self.job(playlist)["status"] == "stopped")
        self.assertEqual(self.job(playlist)["counts"], dict(total=6, complete=1, active=0, queued=0, stopped=5, failed=0))
        self.assertEqual(self.client.post(playlist["status_url"] + "/stop").status_code, 200)
        self.assertEqual(len(list(self.directory.glob("*.mp4"))), 1)
        self.assertEqual(len(list(self.directory.glob("*.part"))), 3)
        for number in (4, 5, 6):
            self.assertFalse((self.directory / f"started-video-entry-{number}").exists())

    def test_stop_during_discovery_prevents_later_entries(self):
        playlist = self.playlist("-slow")
        self.wait_for(lambda: (self.directory / "discovery-arrived").exists())
        self.client.post(playlist["status_url"] + "/stop")
        self.wait_for(lambda: self.job(playlist)["status"] == "stopped")
        (self.directory / "release-discovery").touch()
        self.assertEqual(self.job(playlist)["counts"]["total"], 2)
        self.assertFalse((self.directory / "started-video-entry-3").exists())

    def test_shared_videos_survive_stop_of_another_playlist(self):
        standalone = self.video(1)
        first = self.playlist("-first")
        second = self.playlist("-second")
        self.wait_for(lambda: self.job(first)["discovery_done"] and self.job(second)["discovery_done"])
        self.assertEqual(self.job(first)["entries"], self.job(second)["entries"])
        self.client.post(first["status_url"] + "/stop")
        self.wait_for(lambda: self.job(first)["status"] == "stopped")
        self.assertEqual(self.job(first)["counts"]["stopped"], 6)
        self.assertEqual(self.job(second)["counts"]["stopped"], 0)
        self.client.post(second["status_url"] + "/stop")
        self.wait_for(lambda: self.job(second)["status"] == "stopped")
        self.assertEqual(self.job(standalone)["status"], "downloading")
        self.assertEqual([job["id"] for job in web.jobs], [standalone["id"]])

    def test_task_and_live_finalization_share_the_limit(self):
        first = self.submit("https://example.test/live-one").get_json()["job"]
        second = self.submit("https://example.test/live-two").get_json()["job"]
        self.wait_for(lambda: all(self.job(job)["can_stop"] for job in (first, second)))
        task = self.create_task(mode="download", url="https://example.test/video-entry-1")
        checked = self.run_task(task)
        self.assertEqual(checked["last_result"], "started")
        queued = web.find_job(checked["last_job_id"])
        self.wait_for(lambda: queued["status"] == "queued")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.run_task(task)["last_result"], "already_running")
        self.client.post(first["status_url"] + "/stop")
        self.wait_for(lambda: self.job(first)["status"] == "finalizing")
        self.assertEqual(queued["status"], "queued")
        (self.directory / "release-finalizing").touch()
        self.wait_for(lambda: queued["status"] == "downloading")
        self.assertEqual(self.job(second)["status"], "recording")

    def test_task_reuse_keeps_a_playlist_video_running(self):
        playlist = self.playlist()
        self.wait_for(lambda: self.job(playlist)["discovery_done"])
        task = self.create_task(mode="download", url="https://example.test/video-entry-1")
        checked = self.run_task(task)
        self.assertEqual(checked["last_result"], "already_running")
        self.client.post(playlist["status_url"] + "/stop")
        self.wait_for(lambda: self.job(playlist)["status"] == "stopped")
        retained = web.find_job(checked["last_job_id"])
        self.wait_for(lambda: retained["status"] == "downloading")
        self.assertEqual([job["id"] for job in web.jobs], [retained["id"]])

    def test_listing_failure_keeps_discovered_downloads(self):
        playlist = self.playlist("-broken")
        self.wait_for(lambda: self.job(playlist)["discovery_done"])
        self.assertEqual(self.job(playlist)["error"], "Playlist listing failed")
        self.release(1); self.release(2)
        self.wait_for(lambda: self.job(playlist)["status"] == "failed")
        self.assertEqual(self.job(playlist)["counts"]["complete"], 2)

    def test_restart_preserves_playlist_members_and_marks_queue_interrupted(self):
        now = time.time()
        child = dict(id="queued-child", url="https://example.test/video-queued", kind="video",
                     status="queued", title="Queued video", progress="Queued", created_at=now, finished_at=None,
                     live=False, playlist_id="parent")
        parent = dict(id="parent", url="https://example.test/playlist", kind="playlist", title="Playlist",
                      status="downloading", progress="0 of 1 complete", created_at=now, finished_at=None,
                      live=False, entries=[{"id": child["id"]}], discovery_done=True)
        web.store.save_job(child); web.store.save_job(parent)
        web.scheduler.stop(); web.store.close()
        web.store = None; web.scheduler = None
        web.init_runtime()
        for job in (child, parent):
            self.assertEqual(web.find_job(job["id"])["status"], "interrupted")
        result = self.client.get("/api/v1/downloads/parent").get_json()["job"]
        self.assertEqual(result["entries"], parent["entries"])
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertFalse(web.jobs)

    def test_invalid_limit_fails_before_creating_state(self):
        for value in ("0", "-1", "1.5", "many"):
            directory = self.directory / f"invalid-{value}"
            env = dict(os.environ, YTDLP_DOWNLOAD_DIR=str(self.directory), YTDLP_STATE_DIR=str(directory),
                       YTDLP_MAX_CONCURRENT_DOWNLOADS=value)
            result = subprocess.run([sys.executable, str(ROOT / "app.py")], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("YTDLP_MAX_CONCURRENT_DOWNLOADS must be a positive integer", result.stderr)
            self.assertFalse(directory.exists())
        with patch.dict(os.environ, {"YTDLP_MAX_CONCURRENT_DOWNLOADS": ""}):
            self.assertEqual(web.download_limit(), 3)
