"""Exercise the real web/download path against local, generated media only."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time
import unittest
import wave

# This file is dispatched through the entrypoint in a fresh interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as web


def audio_fixture(seconds=1):
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * seconds)
    return output.getvalue()


MEDIA = audio_fixture()
SECOND_MEDIA = MEDIA[:-2] + b"\x01\x00"
LARGE_MEDIA = audio_fixture(60)


class MediaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metadata/playlist-next":
            self.server.listing_arrived.set()
            if not self.server.release_listing.wait(15):
                self.send_error(503, "Playlist discovery was not released")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"next playlist page")
            return
        if self.path == "/media/failure.wav":
            self.send_error(404, "Fixture media does not exist")
            return
        if self.path.startswith("/media/cancel-"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(LARGE_MEDIA)))
            self.end_headers()
            try:
                self.wfile.write(LARGE_MEDIA[:32768])
                self.wfile.flush()
                with self.server.arrivals_lock:
                    self.server.arrivals += 1
                    if self.server.arrivals >= 2:
                        self.server.both_arrived.set()
                if self.server.release.wait(15):
                    self.wfile.write(LARGE_MEDIA[32768:])
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.startswith(("/media/parallel-", "/media/collision-")):
            with self.server.arrivals_lock:
                self.server.arrivals += 1
                if self.server.arrivals == 2:
                    self.server.both_arrived.set()
            if not self.server.release.wait(10):
                self.send_error(503, "Concurrent downloads did not arrive")
                return
        self.send_response(200)
        media = SECOND_MEDIA if self.path == "/media/collision-two.wav" else MEDIA
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(media)))
        self.end_headers()
        self.wfile.write(media)

    def log_message(self, *args):
        pass


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = TemporaryDirectory(prefix="yt-dlp-web-downloads-")
        cls.original_download_dir = web.DOWNLOAD_DIR
        # Child workers receive this destination; no downloader calls are mocked.
        web.DOWNLOAD_DIR = cls.output.name
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
        cls.server.arrivals_lock = Lock()
        cls.server.arrivals = 0
        cls.server.both_arrived = Event()
        cls.server.release = Event()
        cls.server.listing_arrived = Event()
        cls.server.release_listing = Event()
        cls.server_thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        web.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        web.DOWNLOAD_DIR = cls.original_download_dir
        cls.output.cleanup()

    def setUp(self):
        with web.lock:
            self.assertFalse(web.jobs, "Previous jobs must finish before the next check")
            web.complete.clear()
            web.stopped.clear()
            web.errors.clear()
        self.server.arrivals = 0
        self.server.both_arrived.clear()
        self.server.release.clear()
        self.server.listing_arrived.clear()
        self.server.release_listing.clear()
        self.client = web.app.test_client()

    def submit(self, path):
        response = self.client.post("/", data={"url": self.base_url + path})
        self.assertEqual(response.status_code, 302)

    def wait_for_jobs(self, completed, errors=0):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with web.lock:
                if not web.jobs:
                    self.assertEqual(len(web.complete), completed, repr(web.errors))
                    self.assertEqual(len(web.errors), errors, repr(web.errors))
                    return list(web.complete)
            time.sleep(0.025)
        self.fail("Local fixture download did not finish within 20 seconds")

    def assert_served(self, job, media=MEDIA):
        self.assertEqual(job["progress"], "100%")
        self.assertTrue(Path(job["file"]).is_relative_to(self.output.name))
        with self.client.get(f"/download/{job['id']}") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, media)
            self.assertIn("inline;", response.headers["Content-Disposition"])
        with self.client.get(f"/download/{job['id']}", headers={"Range": "bytes=10-29"}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, media[10:30])
            self.assertEqual(response.headers["Content-Range"], f"bytes 10-29/{len(media)}")
        with self.client.get(f"/api/v1/downloads/{job['id']}/file") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, media)
            self.assertIn("attachment;", response.headers["Content-Disposition"])

    def test_direct_ordinary_download_and_file_serving(self):
        self.submit("/ordinary.wav")
        [job] = self.wait_for_jobs(1)
        self.assert_served(job)
        status = self.client.get("/status").get_data(as_text=True)
        self.assertIn(f"/download/{job['id']}", status)
        self.assertIn(f'/download/{job["id"]}" target="_blank" rel="noopener"', status)
        self.assertIn("100%", status)
        Path(job["file"]).unlink()
        self.assertEqual(self.client.get(f"/download/{job['id']}").status_code, 404)
        self.assertEqual(self.client.get("/download/unknown").status_code, 404)

    def test_stop_keeps_partial_files_and_other_download_continues(self):
        self.submit("/fixture/cancel-one")
        self.submit("/fixture/cancel-two")
        try:
            self.assertTrue(self.server.both_arrived.wait(10))
            with web.lock:
                first = next(job for job in web.jobs if job["url"].endswith("cancel-one"))
                second = next(job for job in web.jobs if job["url"].endswith("cancel-two"))
                worker = first["worker"]
            deadline = time.monotonic() + 5
            while first["progress"] == "0%" and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertGreater(float(first["progress"].rstrip("%")), 0)
            self.assertTrue(self.client.get(f"/api/v1/downloads/{first['id']}").get_json()["job"]["can_stop"])
            self.assertIn(f'/stop/{first["id"]}', self.client.get("/status").get_data(as_text=True))
            self.assertEqual(self.client.post(f"/api/v1/downloads/{first['id']}/stop").status_code, 202)
            deadline = time.monotonic() + 5
            while first in web.jobs and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertIn(first, web.stopped)
            self.assertIsNotNone(worker.poll())
            self.assertEqual(self.client.post(f"/api/v1/downloads/{first['id']}/stop").status_code, 200)
            self.assertIn(second, web.jobs)
            self.assertFalse(second["stopping"])
            self.assertIsNone(second["worker"].poll())
            [partial] = list(Path(self.output.name).glob(f"*{first['id']}*.part"))
            retained = partial.read_bytes()
            self.assertGreater(len(retained), 0)
            self.assertLess(len(retained), len(LARGE_MEDIA))
            self.assertEqual(retained, LARGE_MEDIA[:len(retained)])
            self.assertEqual(self.client.get(f"/download/{first['id']}").status_code, 404)
        finally:
            self.server.release.set()
        [completed] = self.wait_for_jobs(1)
        self.assertEqual(completed["id"], second["id"])
        self.assert_served(completed, LARGE_MEDIA)
        self.assertEqual(partial.read_bytes(), retained)

    def test_native_plugin_concurrent_downloads(self):
        self.submit("/fixture/parallel-one")
        self.submit("/fixture/parallel-two")
        try:
            self.assertTrue(self.server.both_arrived.wait(10), "Plugin jobs did not download concurrently")
            with web.lock:
                self.assertEqual(len(web.jobs), 2)
                self.assertEqual(len({job["id"] for job in web.jobs}), 2)
            status = self.client.get("/status").get_data(as_text=True)
            self.assertIn("Container fixture parallel-one", status)
            self.assertIn("Container fixture parallel-two", status)
        finally:
            self.server.release.set()
        finished = self.wait_for_jobs(2)
        self.assertEqual(len({job["file"] for job in finished}), 2)
        for job in finished:
            self.assert_served(job)

    def test_real_playlist_discovers_while_individual_videos_download(self):
        self.submit("/fixture/playlist")
        try:
            self.assertTrue(self.server.listing_arrived.wait(10), "Discovery did not request the next page")
            self.assertTrue(self.server.both_arrived.wait(10), "Playlist videos did not download concurrently")
            with web.lock:
                playlist = next(job for job in web.jobs if job.get("kind") == "playlist")
                self.assertEqual(web.playlist_counts(playlist)["active"], 2)
                self.assertFalse(playlist["discovery_done"])
            self.server.release.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with web.lock:
                    if web.playlist_counts(playlist)["complete"] == 2:
                        break
                time.sleep(.025)
            self.assertEqual(web.playlist_counts(playlist)["complete"], 2)
            self.assertFalse(playlist["discovery_done"])
        finally:
            self.server.release.set()
            self.server.release_listing.set()
        finished = self.wait_for_jobs(4)
        self.assertEqual(playlist["status"], "complete")
        self.assertEqual(web.playlist_counts(playlist)["total"], 3)
        self.assertEqual(self.client.get(f'/download/{playlist["id"]}').status_code, 404)
        files = [job for job in finished if job.get("kind") != "playlist"]
        self.assertEqual(len(files), 3)
        for job in files:
            self.assert_served(job)

    def test_download_failure_is_not_completed(self):
        self.submit("/fixture/failure")
        self.wait_for_jobs(0, errors=1)
        status = self.client.get("/status").get_data(as_text=True)
        self.assertIn("Failed", status)
        self.assertIn("404", status)
        failed_job, _ = web.errors[0]
        self.assertEqual(self.client.get(f"/download/{failed_job['id']}").status_code, 404)
        response = self.client.post("/api/v1/downloads", json={"url": failed_job["url"]})
        self.assertEqual(response.status_code, 202)
        self.assertNotEqual(response.get_json()["job"]["id"], failed_job["id"])
        self.wait_for_jobs(0, errors=2)

    def test_concurrent_matching_titles_keep_both_files(self):
        self.submit("/fixture/collision-one")
        self.submit("/fixture/collision-two")
        try:
            self.assertTrue(self.server.both_arrived.wait(10), "Matching-title downloads did not run concurrently")
        finally:
            self.server.release.set()
        finished = self.wait_for_jobs(2)
        self.assertEqual({Path(job["file"]).name for job in finished}, {
            "Container fixture collision 2026-09-16 00_26.wav",
            "Container fixture collision 2026-09-16 00_26 (2).wav",
        })
        for job in finished:
            self.assert_served(job, SECOND_MEDIA if job["url"].endswith("collision-two") else MEDIA)

    def test_repeated_download_keeps_both_files(self):
        url = self.base_url + "/fixture/repeated"
        first_response = self.client.post("/api/v1/downloads", json={"url": url})
        self.assertEqual(first_response.status_code, 202)
        [first] = self.wait_for_jobs(1)
        first_path = Path(first["file"])
        first_stat = first_path.stat()

        second_response = self.client.post("/api/v1/downloads", json={"url": url})
        self.assertEqual(second_response.status_code, 202)
        self.assertTrue(second_response.get_json()["created"])
        self.assertNotEqual(second_response.get_json()["job"]["id"], first["id"])
        finished = self.wait_for_jobs(2)
        self.assertEqual(len({job["file"] for job in finished}), 2)
        self.assertEqual(first_path.stat().st_mtime_ns, first_stat.st_mtime_ns)
        names = {Path(job["file"]).name for job in finished}
        self.assertEqual(names, {
            "Container fixture repeated 2026-09-16 00_26.wav",
            "Container fixture repeated 2026-09-16 00_26 (2).wav",
        })
        for job in finished:
            self.assert_served(job)
            with self.client.get(f"/api/v1/downloads/{job['id']}/file") as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, MEDIA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
