"""Stop real, concurrent FFmpeg HLS recordings without external services.

Dispatched in a fresh interpreter by test_runtime.py for both selected packages.
The fixture generator and every recording use temporary files, never /downloads.
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, Thread
import time
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as web


def wav_fixture():
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    return output.getvalue()


MEDIA = wav_fixture()


class MediaHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metadata/flip":
            self.server.flip_calls += 1
            body = b"live" if self.server.flip_calls == 1 else b"offline"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/metadata/delayed":
            arrived, release, body = self.server.metadata_arrived, self.server.metadata_release, b"ready"
        elif self.path == "/media/vod-blocked.wav":
            arrived, release, body = self.server.vod_arrived, self.server.vod_release, MEDIA
        else:
            return super().do_GET()
        arrived.set()
        if not release.wait(30):
            self.send_error(503, "Fixture was not released")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav" if body == MEDIA else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class LiveStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="yt-dlp-web-live-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = Path(cls.temp.name) / "source"
        cls.source.mkdir()
        cls.output = Path(cls.temp.name) / "downloads"
        cls.output.mkdir()
        cls.original_download_dir = web.DOWNLOAD_DIR
        web.DOWNLOAD_DIR = str(cls.output)
        cls.addClassCleanup(setattr, web, "DOWNLOAD_DIR", cls.original_download_dir)
        cls.generator_log = TemporaryFile(mode="w+b")
        cls.addClassCleanup(cls.generator_log.close)
        cls.generator = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-re", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
                "-g", "10", "-sc_threshold", "0", "-c:a", "aac",
                "-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
                "-hls_flags", "delete_segments+independent_segments+temp_file",
                "-hls_segment_filename", str(cls.source / "segment-%05d.ts"),
                str(cls.source / "live.m3u8"),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=cls.generator_log,
        )
        cls.addClassCleanup(cls.stop_generator)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            playlist = cls.source / "live.m3u8"
            if playlist.exists() and playlist.read_text().count("#EXTINF") >= 3:
                break
            if cls.generator.poll() is not None:
                cls.generator_log.seek(0)
                raise RuntimeError(cls.generator_log.read().decode())
            time.sleep(0.05)
        else:
            raise RuntimeError("The local HLS generator did not become ready")

        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(MediaHandler, directory=str(cls.source)),
        )
        for name in ("metadata_arrived", "metadata_release", "vod_arrived", "vod_release"):
            setattr(cls.server, name, Event())
        cls.server_thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.server.flip_calls = 0
        cls.server_thread.start()
        cls.addClassCleanup(cls.stop_server)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        web.app.config["TESTING"] = True

    @classmethod
    def stop_generator(cls):
        if cls.generator.poll() is None:
            cls.generator.stdin.write(b"q\n")
            cls.generator.stdin.flush()
            try:
                cls.generator.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Only the test-owned synthetic source; never a recording worker.
                cls.generator.terminate()
                cls.generator.wait(timeout=5)
        cls.generator.stdin.close()

    @classmethod
    def stop_server(cls):
        cls.server.metadata_release.set()
        cls.server.vod_release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        with web.lock:
            self.assertFalse(web.jobs, "The preceding check left an active recording")
            web.complete.clear()
            web.errors.clear()
        for name in ("metadata_arrived", "metadata_release", "vod_arrived", "vod_release"):
            getattr(self.server, name).clear()
        self.client = web.app.test_client()
        self.started = []
        self.workers = set()

    def tearDown(self):
        self.server.metadata_release.set()
        self.server.vod_release.set()
        # Also release recorders if an assertion fails. Test cleanup uses the same
        # graceful path as the UI and never kills a recording or its FFmpeg child.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with web.lock:
                active = list(web.jobs)
            if not active:
                break
            for job in active:
                if job.get("worker") is not None:
                    self.workers.add(job["worker"])
                if job.get("stoppable") and not job.get("stopping"):
                    self.client.post(f"/stop/{job['id']}")
            time.sleep(0.05)
        with web.lock:
            self.assertFalse(web.jobs, "Graceful fixture cleanup left active workers")
        for worker in self.workers:
            self.assertIsNotNone(worker.poll(), "Recording worker was not reaped")

    def submit(self, name):
        with web.lock:
            previous = {job["id"] for job in web.jobs}
        response = self.client.post("/", data={"url": f"{self.base_url}/fixture/{name}"})
        self.assertEqual(response.status_code, 302)
        with web.lock:
            [job] = [job for job in web.jobs if job["id"] not in previous]
        self.started.append(job)
        return job

    def wait_until(self, predicate, message, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with web.lock:
                if predicate():
                    return
                if web.errors:
                    self.fail(f"{message}: {web.errors!r}")
            time.sleep(0.025)
        self.fail(f"{message}: {self.client.get('/status').get_data(as_text=True)}")

    def wait_recording(self, job):
        self.wait_until(
            lambda: job in web.jobs and job.get("live") and job.get("stoppable"),
            "Live recording did not become stoppable",
        )
        self.assertIsNone(job["worker"].poll())
        self.workers.add(job["worker"])

    def stop(self, job):
        # Repeated requests can race FFmpeg exit or postprocessing. Both must be
        # idempotent, including after the completed file has been registered.
        worker = job["worker"]
        for _ in range(2):
            response = self.client.post(f"/stop/{job['id']}")
            self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        self.wait_until(lambda: job in web.complete, "Stopped recording did not complete")
        self.assertEqual(worker.wait(timeout=5), 0)
        self.assertEqual(self.client.post(f"/stop/{job['id']}").status_code, 303)

    def assert_playable(self, job):
        path = Path(job["file"])
        self.assertTrue(path.is_relative_to(self.output))
        self.assertEqual(path.suffix, ".mp4")
        self.assertGreater(path.stat().st_size, 1000)
        self.assertTrue(job["progress"].startswith("Recorded - "), job["progress"])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True, text=True, capture_output=True, timeout=10,
        )
        info = json.loads(probe.stdout)
        self.assertIn("mp4", info["format"]["format_name"].split(","))
        self.assertEqual({stream["codec_type"] for stream in info["streams"]}, {"video", "audio"})
        duration = float(info["format"]["duration"])
        self.assertGreater(duration, 1)
        self.assertAlmostEqual(job["duration"], duration, delta=0.05)
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr)
        with self.client.get(f"/download/{job['id']}") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, path.read_bytes())
            self.assertIn("inline;", response.headers["Content-Disposition"])
        return duration

    def test_only_selected_live_recording_stops_and_both_videos_play(self):
        first = self.submit("live-one")
        second = self.submit("live-two")
        self.wait_recording(first)
        self.wait_recording(second)
        self.assertNotEqual(first["worker"].pid, second["worker"].pid)
        status = self.client.get("/status").get_data(as_text=True)
        for job in (first, second):
            self.assertIn(f'/stop/{job["id"]}', status)
        # Let FFmpeg finish input probing and mux some packets before requesting q.
        time.sleep(5)
        self.stop(first)
        first_duration = self.assert_playable(first)
        with web.lock:
            self.assertIn(second, web.jobs)
            self.assertFalse(second["stopping"])
        self.assertIsNone(second["worker"].poll())
        time.sleep(4)
        self.stop(second)
        second_duration = self.assert_playable(second)
        self.assertGreater(second_duration, first_duration + 1)
        self.assertNotEqual(first["file"], second["file"])

    def test_stop_before_metadata_is_clear_and_retry_is_safe(self):
        job = self.submit("live-delayed")
        self.assertTrue(self.server.metadata_arrived.wait(15))
        response = self.client.post(f"/stop/{job['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_data(as_text=True).strip())
        self.assertEqual(self.client.get(f"/stop/{job['id']}").status_code, 405)
        self.assertEqual(self.client.post("/stop/unknown").status_code, 404)
        with web.lock:
            self.assertFalse(job["stopping"])
        self.server.metadata_release.set()
        self.wait_recording(job)
        time.sleep(5)
        self.stop(job)
        self.assert_playable(job)

    def test_vod_rejects_stop_without_interrupting_download(self):
        job = self.submit("vod-blocked")
        self.assertTrue(self.server.vod_arrived.wait(15))
        self.workers.add(job["worker"])
        self.assertEqual(self.client.post(f"/stop/{job['id']}").status_code, 409)
        with web.lock:
            self.assertFalse(job["live"])
            self.assertFalse(job["stopping"])
            self.assertIn(job, web.jobs)
        self.assertNotIn(f'/stop/{job["id"]}', self.client.get("/status").get_data(as_text=True))
        self.server.vod_release.set()
        self.wait_until(lambda: job in web.complete, "Rejected Stop interrupted an ordinary download")
        self.assertEqual(Path(job["file"]).read_bytes(), MEDIA)

    def test_task_live_check_records_once_and_api_stop_finalizes(self):
        url = self.base_url + "/fixture/live-task"
        response = self.client.post("/api/v1/tasks", json={"url": url, "enabled": False})
        self.assertEqual(response.status_code, 201)
        task_id = response.get_json()["task"]["id"]
        self.assertEqual(self.client.post(f"/api/v1/tasks/{task_id}/run").status_code, 202)
        self.wait_until(lambda: not web.scheduler.get(task_id)["checking"], "Task check did not finish")
        self.assertEqual(web.scheduler.get(task_id)["last_result"], "started")
        [job] = web.jobs
        self.started.append(job)
        self.wait_recording(job)
        for _ in range(3):
            response = self.client.post("/api/v1/downloads", json={"url": url})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["job"]["id"], job["id"])
        self.client.post(f"/api/v1/tasks/{task_id}/run")
        self.wait_until(lambda: not web.scheduler.get(task_id)["checking"], "Repeat check did not finish")
        self.assertEqual(web.scheduler.get(task_id)["last_result"], "already_running")
        self.assertEqual(len(web.jobs), 1)
        time.sleep(5)
        self.assertEqual(self.client.delete(f"/api/v1/tasks/{task_id}").status_code, 204)
        self.assertIsNone(job["worker"].poll(), "Deleting a task must not interrupt its recording")
        self.assertEqual(self.client.post(f"/api/v1/downloads/{job['id']}/stop").status_code, 202)
        self.wait_until(lambda: job in web.complete, "API Stop did not complete")
        self.assert_playable(job)

    def test_offline_and_live_to_vod_race_never_download_vod(self):
        for path, expected in (("task-offline", "offline"), ("vod", "offline")):
            response = self.client.post("/api/v1/tasks", json={"url": self.base_url + "/fixture/" + path, "enabled": False})
            task_id = response.get_json()["task"]["id"]
            self.client.post(f"/api/v1/tasks/{task_id}/run")
            self.wait_until(lambda: not web.scheduler.get(task_id)["checking"], "Offline check did not finish")
            self.assertEqual(web.scheduler.get(task_id)["last_result"], expected, web.scheduler.get(task_id))
            self.assertFalse(web.jobs)
        response = self.client.post("/api/v1/tasks", json={"url": self.base_url + "/fixture/live-flips", "enabled": False})
        task_id = response.get_json()["task"]["id"]
        self.client.post(f"/api/v1/tasks/{task_id}/run")
        self.wait_until(lambda: bool(web.errors), "Live-only worker did not reject the ended stream")
        self.assertIn("no longer live", web.errors[0][1])
        self.assertFalse(web.complete)
        self.assertFalse(web.jobs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
