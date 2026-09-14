"""API contracts, durable ownership, and scheduling races with process barriers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import app as web
from scheduler import next_run, normalize_url
from state import StateStore


ROOT = Path(__file__).resolve().parents[1]


class ApiTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="yt-dlp-web-api-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.patch_env = patch.dict(os.environ, {"YTDLP_SCHEDULER_ENABLED": "0", "YTDLP_STATE_DIR": str(self.directory / "state"),
                                                 "YTDLP_TEST_BARRIER_DIR": str(self.directory)})
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.patch_app = patch.multiple(web, store=None, scheduler=None, DOWNLOAD_DIR=self.directory,
                                       WORKER=ROOT / "tests/fixtures/control_worker.py")
        self.patch_app.start()
        self.addCleanup(self.patch_app.stop)
        self.patch_config = patch.dict(web.app.config, {"TESTING": True})
        self.patch_config.start()
        self.addCleanup(self.patch_config.stop)
        web.jobs.clear(); web.complete.clear(); web.errors.clear()
        web.init_runtime()
        self.client = web.app.test_client()

    def wait_for(self, check):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if check():
                return
            time.sleep(.01)
        self.fail("Condition did not become true before the test deadline")

    def tearDown(self):
        web.scheduler.stop()
        (self.directory / "release-probe").touch()
        (self.directory / "release-finalizing").touch()
        self.wait_for(lambda: not web.scheduler.checking)
        until = time.monotonic() + 10
        while web.jobs and time.monotonic() < until:
            with web.lock:
                for job in list(web.jobs):
                    if job.get("stoppable"):
                        web.stop_download(job["id"])
            time.sleep(.01)
        self.assertFalse(web.jobs, "Test workers did not exit")
        web.store.close()
        web.jobs.clear(); web.complete.clear(); web.errors.clear()

    def submit(self, url="https://example.test/live", key=None):
        return self.client.post("/api/v1/downloads", json={"url": url}, headers={"Idempotency-Key": key} if key else {})

    def create_task(self, **fields):
        data = {"name": "Example", "url": "https://example.test/live", "cron": "*/5 * * * *", "timezone": "UTC", **fields}
        response = self.client.post("/api/v1/tasks", json=data)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["task"]

    def finish(self, job):
        self.wait_for(lambda: web.find_job(job["id"]).get("stoppable"))
        (self.directory / "release-finalizing").touch()
        self.assertEqual(self.client.post(f"/api/v1/downloads/{job['id']}/stop").status_code, 202)
        self.wait_for(lambda: not web.jobs)

    def run_task(self, task):
        response = self.client.post(f"/api/v1/tasks/{task['id']}/run")
        self.assertIn(response.status_code, (200, 202))
        self.wait_for(lambda: not web.scheduler.get(task["id"])["checking"])
        return web.scheduler.get(task["id"])

    def test_parallel_requests_ui_and_completed_idempotency(self):
        def submit(index):
            client = web.app.test_client()
            return client.post("/api/v1/downloads", json={"url": f"https://EXAMPLE.test:443/live#view-{index}"}).get_json()
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(submit, range(24)))
        self.assertEqual(sum(item["created"] for item in responses), 1)
        self.assertEqual(len({item["job"]["id"] for item in responses}), 1)
        job = responses[0]["job"]
        self.assertEqual(self.submit(key="retry-key").get_json()["job"]["id"], job["id"])
        self.assertEqual(self.client.post("/", data={"url": job["url"]}).status_code, 302)
        self.assertEqual(len(web.jobs), 1)
        self.finish(job)
        replay = self.submit(key="retry-key")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()["job"]["id"], job["id"])
        self.assertEqual(self.submit("https://example.test/another", key="retry-key").status_code, 409)
        self.assertEqual(self.client.post(f"/api/v1/downloads/{job['id']}/stop").status_code, 200)
        with self.client.get(f"/api/v1/downloads/{job['id']}/file") as file:
            self.assertEqual(file.status_code, 200)
            self.assertIn(b"controlled test output", file.data)
        self.assertNotIn("file", replay.get_json()["job"])
        self.assertNotIn("worker", replay.get_json()["job"])
        self.assertEqual(self.submit().status_code, 202, "A fresh request may download again after completion")

    def test_url_stays_reserved_through_finalization(self):
        job = self.submit().get_json()["job"]
        self.wait_for(lambda: web.find_job(job["id"]).get("stoppable"))
        self.client.post(f"/api/v1/downloads/{job['id']}/stop")
        self.wait_for(lambda: web.find_job(job["id"])["status"] == "finalizing")
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job"]["id"], job["id"])
        task = self.create_task()
        checked = self.run_task(task)
        self.assertEqual(checked["last_result"], "already_running")
        self.assertEqual(checked["last_job_id"], job["id"])
        self.assertEqual(len(web.jobs), 1)

    def test_routes_and_cross_site_requests(self):
        for path in ("/", "/tasks", "/status", "/api/v1/downloads", "/api/v1/tasks", "/api/v1/health"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("Set-Cookie", response.headers)
        for path in ("/api/v1/downloads/no/file", "/download/no"):
            self.assertEqual(self.client.get(path).status_code, 404)
        with self.client.get("/static/app.css") as response:
            self.assertEqual(response.status_code, 200)
        for headers in ({"Origin": "https://other.test"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.post("/api/v1/tasks", json={}, headers=headers).status_code, 403)
        self.assertFalse(web.jobs)

    def test_server_rendered_task_forms(self):
        response = self.client.post("/tasks", data={
            "name": "HTML task", "url": "https://example.test/offline", "cron": "*/5 * * * *",
            "timezone": "UTC", "mode": "live", "enabled": "on",
        })
        self.assertEqual(response.status_code, 303)
        [task] = web.scheduler.list()
        page = self.client.get("/tasks").get_data(as_text=True)
        self.assertIn("HTML task", page)
        self.assertIn(f'/tasks/{task["id"]}/save', page)
        self.assertNotIn("<script", page)
        self.assertNotIn("iOS Shortcut", page)
        self.assertNotIn("Check a page on a schedule", page)
        self.assertEqual(self.client.get("/static/tasks.js").status_code, 404)

        response = self.client.post(f'/tasks/{task["id"]}/save', data={
            "name": "Edited", "url": task["url"], "cron": task["cron"],
            "timezone": task["timezone"], "mode": task["mode"],
        })
        self.assertEqual(response.status_code, 303)
        self.assertFalse(web.scheduler.get(task["id"])["enabled"])
        self.assertEqual(self.client.post(f'/tasks/{task["id"]}/run').status_code, 303)
        self.wait_for(lambda: not web.scheduler.get(task["id"])["checking"])
        self.assertEqual(web.scheduler.get(task["id"])["last_result"], "offline")
        self.assertEqual(self.client.post(f'/tasks/{task["id"]}/unknown').status_code, 404)
        self.assertEqual(self.client.post(f'/tasks/{task["id"]}/delete').status_code, 303)
        self.assertFalse(web.scheduler.list())

    def test_bad_requests_and_unknown_routes_return_json_errors(self):
        for data in (None, [], {"url": 12}, {"url": "file:///tmp/video"}, {"url": "http://"}, {"url": "https://example.test/a b"},
                     {"url": "https://example.test:bad/video"}, {"url": "https://example.test/live", "command": "oops"}):
            response = self.client.post("/api/v1/downloads", json=data, content_type="application/json")
            self.assertEqual(response.status_code, 400, (data, response.data))
            self.assertIn("error", response.get_json())
        self.assertEqual(self.client.post("/api/v1/downloads", data="not-json").status_code, 415)
        self.assertEqual(self.client.get("/api/v1/downloads?status=nope").status_code, 400)
        for path in ("/api/v1/unknown", "/api/v1/downloads/unknown", "/api/v1/tasks/unknown"):
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertIn("error", self.client.get(path).get_json())
        self.assertFalse(web.jobs)

    def test_task_validation_edit_pause_delete(self):
        invalid = ({"cron": "* * * * * *"}, {"cron": "0 0 31 2 *"}, {"cron": "R * * * *"},
                   {"timezone": "Moon/Base"}, {"mode": "shell"}, {"enabled": "false"}, {"command": "no"})
        for fields in invalid:
            response = self.client.post("/api/v1/tasks", json={"url": "https://example.test/live", **fields})
            self.assertEqual(response.status_code, 400, (fields, response.get_json()))
        task = self.create_task(timezone="Australia/Sydney")
        path = f"/api/v1/tasks/{task['id']}"
        self.assertIn("Z", task["next_run_at"])
        response = self.client.patch(path, json={"enabled": False})
        self.assertIsNone(response.get_json()["task"]["next_run_at"])
        web.scheduler.tick(time.time() + 86400)
        self.assertFalse(web.scheduler.checking)
        self.assertEqual(self.client.patch(path, json={"name": "Edited", "enabled": True}).get_json()["task"]["name"], "Edited")
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_live_offline_upcoming_and_probe_failure(self):
        for mode, expected in (("offline", "offline"), ("upcoming", "offline"), ("probe-error", "error")):
            task = self.create_task(url=f"https://example.test/{mode}")
            checked = self.run_task(task)
            self.assertEqual(checked["last_result"], expected)
            self.assertFalse(web.jobs)
        task = self.create_task()
        self.assertEqual(self.run_task(task)["last_result"], "started")
        self.assertEqual(self.run_task(task)["last_result"], "already_running")
        self.assertEqual(len(web.jobs), 1)
        self.assertTrue(web.jobs[0]["live_only"])

    def test_overlapping_ticks_checks_and_different_tasks_share_ownership(self):
        first = self.create_task(url="https://example.test/delayed")
        second = self.create_task(url=first["url"])
        due = web.scheduler.get(first["id"])["next_run_at"] + 1800
        web.scheduler.tick(due)
        self.wait_for(lambda: (self.directory / "probe-arrived").exists())
        self.assertEqual(self.client.post(f"/api/v1/tasks/{first['id']}/run").status_code, 200)
        web.scheduler.tick(due + 300)
        self.assertEqual(len(web.scheduler.checking), 2)
        self.assertGreater(web.scheduler.get(first["id"])["next_run_at"], due + 300)
        (self.directory / "release-probe").touch()
        self.wait_for(lambda: not web.scheduler.checking)
        self.assertEqual(len(web.jobs), 1)
        self.assertEqual({web.scheduler.get(task["id"])["last_result"] for task in (first, second)}, {"started", "already_running"})

    def test_pause_edit_or_delete_during_probe_prevents_stale_download(self):
        for action in ("pause", "edit", "delete"):
            (self.directory / "release-probe").unlink(missing_ok=True)
            (self.directory / "probe-arrived").unlink(missing_ok=True)
            task = self.create_task(url=f"https://example.test/delayed-{action}")
            path = f"/api/v1/tasks/{task['id']}"
            self.client.post(path + "/run")
            self.wait_for(lambda: (self.directory / "probe-arrived").exists())
            if action == "delete":
                self.client.delete(path)
            else:
                self.client.patch(path, json={"enabled": False} if action == "pause" else {"url": "https://example.test/replacement"})
            (self.directory / "release-probe").touch()
            self.wait_for(lambda: not web.scheduler.checking)
            self.assertFalse(web.jobs)

    def test_download_mode_and_paused_manual_check(self):
        task = self.create_task(mode="download", enabled=False, url="https://example.test/offline")
        self.assertEqual(self.run_task(task)["last_result"], "started")
        self.assertFalse(web.jobs[0]["live_only"])

    def test_restart_preserves_tasks_files_keys_and_recovers_interrupted_jobs(self):
        task = self.create_task(enabled=False)
        job = self.submit(key="persisted-request").get_json()["job"]
        self.finish(job)
        stale = dict(web.complete[0], id="interrupted-fixture", url="https://example.test/interrupted", status="recording")
        web.store.save_job(stale)
        web.scheduler.stop(); web.store.close()
        web.store = None; web.scheduler = None
        web.jobs.clear(); web.complete.clear(); web.errors.clear()
        web.init_runtime()
        self.assertEqual(web.scheduler.get(task["id"])["url"], task["url"])
        self.assertEqual(self.submit(key="persisted-request").get_json()["job"]["id"], job["id"])
        self.assertEqual(self.client.get("/api/v1/downloads/interrupted-fixture").get_json()["job"]["status"], "interrupted")
        self.assertFalse(web.jobs)
        with self.client.get(f"/api/v1/downloads/{job['id']}/file") as response:
            self.assertEqual(response.status_code, 200)

    def test_process_ownership_outlives_parent_descriptor(self):
        job = self.submit().get_json()["job"]
        self.wait_for(lambda: web.find_job(job["id"]).get("stoppable"))
        web.store.close()
        with self.assertRaisesRegex(RuntimeError, "already in use"):
            StateStore(self.directory / "state")
        self.finish(job)
        replacement = StateStore(self.directory / "state")
        replacement.close()

    def test_timezone_cron_and_url_canonicalization(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        due = next_run("0 9 * * *", "Australia/Sydney", base)
        self.assertEqual(datetime.fromtimestamp(due, timezone.utc).isoformat(), "2026-01-01T22:00:00+00:00")
        # Standard CRON joins restricted day-of-month and weekday with OR.
        due = next_run("0 0 2 * sun", "UTC", base)
        self.assertEqual(datetime.fromtimestamp(due, timezone.utc).day, 2)
        next_run("0 0 * mar fri", "UTC", base)
        self.assertEqual(normalize_url(" HTTPS://Example.TEST:443/video?a=1#view "), "https://example.test/video?a=1")
        self.assertEqual(normalize_url("http://[::1]:80"), "http://[::1]/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
