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


def audio_fixture():
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    return output.getvalue()


MEDIA = audio_fixture()


class MediaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/media/failure.wav":
            self.send_error(404, "Fixture media does not exist")
            return
        if self.path.startswith("/media/parallel-"):
            with self.server.arrivals_lock:
                self.server.arrivals += 1
                if self.server.arrivals == 2:
                    self.server.both_arrived.set()
            if not self.server.release.wait(10):
                self.send_error(503, "Concurrent downloads did not arrive")
                return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(MEDIA)))
        self.end_headers()
        self.wfile.write(MEDIA)

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
            web.errors.clear()
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

    def assert_served(self, job):
        self.assertEqual(job["progress"], "100%")
        self.assertTrue(Path(job["file"]).is_relative_to(self.output.name))
        with self.client.get(f"/download/{job['id']}") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, MEDIA)
            self.assertIn("attachment;", response.headers["Content-Disposition"])

    def test_direct_ordinary_download_and_file_serving(self):
        self.submit("/ordinary.wav")
        [job] = self.wait_for_jobs(1)
        self.assert_served(job)
        status = self.client.get("/status").get_data(as_text=True)
        self.assertIn(f"/download/{job['id']}", status)
        self.assertIn("100%", status)
        Path(job["file"]).unlink()
        self.assertEqual(self.client.get(f"/download/{job['id']}").status_code, 404)
        self.assertEqual(self.client.get("/download/unknown").status_code, 404)

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
        for job in finished:
            self.assert_served(job)
            with self.client.get(f"/api/v1/downloads/{job['id']}/file") as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, MEDIA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
