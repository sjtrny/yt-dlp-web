"""Persistent CRON tasks sharing the application's download admission lock."""

from datetime import datetime
import logging
import re
from threading import Event, Thread
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter, CroniterError


def normalize_url(value):
    if not isinstance(value, str):
        raise ValueError("url must be an HTTP or HTTPS URL")
    value = value.strip()
    if not value or len(value) > 4096 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("url must be an HTTP or HTTPS URL without spaces")
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None and parts.port != (443 if parts.scheme == "https" else 80):
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        raise ValueError("url must be an HTTP or HTTPS URL without embedded credentials") from None


def next_run(cron, timezone, after):
    if not isinstance(cron, str) or len(cron) > 200 or len(cron.split()) != 5:
        raise ValueError("cron must have five fields: minute hour day month weekday")
    # No random/hashed extensions: a schedule must be repeatable after restart.
    numeric = re.sub(r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|sun|mon|tue|wed|thu|fri|sat", "1", cron.lower())
    if not re.fullmatch(r"[0-9*/ ,\-]+", numeric):
        raise ValueError("Use a standard five-field CRON expression")
    if not isinstance(timezone, str):
        raise ValueError("timezone must be an IANA name, such as UTC or Australia/Sydney")
    try:
        zone = ZoneInfo(timezone)
        base = datetime.fromtimestamp(after, zone)
        return croniter(cron, base, max_years_between_matches=8).get_next(datetime).timestamp()
    except (ZoneInfoNotFoundError, ValueError, CroniterError):
        raise ValueError("Invalid CRON expression or time zone, or no occurrence within eight years") from None


class TaskScheduler:
    def __init__(self, store, lock, submit, active, probe, *, default_timezone="UTC"):
        self.store, self.lock = store, lock
        self.submit, self.active, self.probe = submit, active, probe
        self.default_timezone = default_timezone
        self.tasks = store.load_tasks()
        self.checking = set()
        self.stop_event = Event()
        self.thread = None
        for task in self.tasks.values():
            if task["last_result"] == "checking":
                task.update(last_result="interrupted", last_error="The previous check was interrupted by a restart.")
                store.save_task(task)

    def list(self):
        with self.lock:
            return [dict(task, checking=task["id"] in self.checking) for task in self.tasks.values()]

    def get(self, task_id):
        with self.lock:
            if task_id not in self.tasks:
                raise KeyError(task_id)
            return dict(self.tasks[task_id], checking=task_id in self.checking)

    def save(self, data, task_id=None):
        allowed = {"name", "url", "cron", "timezone", "mode", "enabled"}
        if not isinstance(data, dict) or data.keys() - allowed:
            raise ValueError("Task fields are name, url, cron, timezone, mode, and enabled")
        with self.lock:
            if task_id is not None and task_id not in self.tasks:
                raise KeyError(task_id)
            now = time.time()
            task = dict(self.tasks[task_id]) if task_id else {
                "id": uuid4().hex, "name": "", "url": "", "cron": "*/5 * * * *",
                "timezone": self.default_timezone, "mode": "live", "enabled": True, "created_at": now,
                "last_checked_at": None, "last_result": "never", "last_error": None,
                "last_job_id": None, "revision": 0,
            }
            task.update(data)
            task["url"] = normalize_url(task["url"])
            if not isinstance(task["name"], str) or len(task["name"]) > 120:
                raise ValueError("name must be text of at most 120 characters")
            task["name"] = task["name"].strip() or task["url"][:120]
            if task["mode"] not in ("live", "download"):
                raise ValueError("mode must be live or download")
            if type(task["enabled"]) is not bool:
                raise ValueError("enabled must be a JSON boolean")
            due = next_run(task["cron"], task["timezone"], now)
            task.update(next_run_at=due if task["enabled"] else None, updated_at=now, revision=task["revision"] + 1)
            self.store.save_task(task)
            self.tasks[task["id"]] = task
            return dict(task)

    def delete(self, task_id):
        with self.lock:
            if task_id not in self.tasks:
                raise KeyError(task_id)
            self.store.delete_task(task_id)
            del self.tasks[task_id]

    def check(self, task_id, *, scheduled=False, now=None):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            now = time.time() if now is None else now
            if scheduled:
                if not task["enabled"] or task["next_run_at"] > now:
                    return False
                # Coalesce missed intervals; never queue a backlog of checks.
                task["next_run_at"] = next_run(task["cron"], task["timezone"], now)
                self.store.save_task(task)
            if task_id in self.checking:
                return False
            task.update(last_checked_at=now, last_result="checking", last_error=None)
            self.store.save_task(task)
            self.checking.add(task_id)
            snapshot = dict(task)
            try:
                Thread(target=self._check, args=(snapshot,), daemon=True, name=f"task-{task_id[:8]}").start()
            except Exception:
                self.checking.discard(task_id)
                task.update(last_result="error", last_error="Could not start the check worker.")
                self.store.save_task(task)
                raise
            return True

    def _check(self, snapshot):
        result, error, job_id = "offline", None, None
        try:
            with self.lock:
                existing = self.active(snapshot["url"])
            if existing:
                result, job_id = "already_running", existing["id"]
            else:
                live = snapshot["mode"] == "download" or self.probe(snapshot["url"])
                with self.lock:
                    current = self.tasks.get(snapshot["id"])
                    if current is None or current["revision"] != snapshot["revision"] or self.stop_event.is_set():
                        result = "cancelled"
                    elif live:
                        job, created = self.submit(snapshot["url"], task_id=snapshot["id"], live_only=snapshot["mode"] == "live")
                        result, job_id = ("started" if created else "already_running"), job["id"]
        except Exception as exc:
            result, error = "error", str(exc)
        finally:
            with self.lock:
                self.checking.discard(snapshot["id"])
                task = self.tasks.get(snapshot["id"])
                if task is not None:
                    if task["revision"] != snapshot["revision"]:
                        result, error, job_id = "cancelled", None, None
                    task.update(last_result=result, last_error=error, last_job_id=job_id)
                    self.store.save_task(task)

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for task_id in list(self.tasks):
                task = self.tasks[task_id]
                if task["enabled"] and task["next_run_at"] <= now:
                    self.check(task_id, scheduled=True, now=now)

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    logging.exception("Task scheduler could not run its check")
                self.stop_event.wait(1)
        self.thread = Thread(target=loop, daemon=True, name="task-scheduler")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
