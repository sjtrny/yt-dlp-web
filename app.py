from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
from threading import RLock, Thread
import time
from urllib.parse import urlsplit
from uuid import uuid4

from flask import abort, Flask, jsonify, redirect, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from scheduler import normalize_url, TaskScheduler
from state import ACTIVE_STATES, StateStore

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16384
jobs = []
complete = []
errors = []
lock = RLock()
TITLE_MAX = 80
DOWNLOAD_DIR = Path(os.environ.get("YTDLP_DOWNLOAD_DIR", "/downloads"))
WORKER = Path(__file__).with_name("recording_worker.py")
store = None
scheduler = None


def init_runtime():
    global store, scheduler
    with lock:
        if store is not None:
            return
        destination = Path(DOWNLOAD_DIR)
        try:
            with TemporaryFile(dir=destination) as probe:
                probe.write(b"write check")
                probe.flush()
        except OSError as error:
            raise RuntimeError(f"Cannot write to {destination}: {error}. Check the download mount and its permissions.") from error
        candidate = StateStore(os.environ.get("YTDLP_STATE_DIR") or destination / ".yt-dlp-web")
        for job in candidate.load_jobs():
            if job["status"] in ACTIVE_STATES:
                job.update(status="interrupted", stoppable=False, stopping=False, finished_at=time.time(),
                           error="The application stopped before this download completed. Partial files were retained.")
                candidate.save_job(job)
            if job["status"] == "complete":
                complete.append(job)
            else:
                errors.append((job, job.get("error", "Download interrupted")))
        store = candidate
        scheduler = TaskScheduler(
            store, lock, submit_download, active_download, probe_live,
            default_timezone=os.environ.get("YTDLP_DEFAULT_TIMEZONE") or "UTC",
        )
        scheduler.start()


def active_download(url):
    with lock:
        return next((job for job in jobs if job["url"] == url), None)


def find_job(job_id):
    with lock:
        job = next((item for item in [*jobs, *complete, *(item[0] for item in errors)] if item["id"] == job_id), None)
        if job is None:
            abort(404, "Download not found")
        return job


def submit_download(url, *, task_id=None, live_only=False):
    url = normalize_url(url)
    with lock:
        existing = active_download(url)
        if existing:
            return existing, False
        job = {"id": uuid4().hex, "url": url, "title": "Loading…", "progress": "0%",
               "live": False, "stoppable": False, "stopping": False, "status": "starting",
               "created_at": time.time(), "finished_at": None, "task_id": task_id, "live_only": live_only}
        store.save_job(job)
        jobs.append(job)
        try:
            Thread(target=download, args=(job,), daemon=True, name=f"download-{job['id'][:8]}").start()
        except Exception as error:
            jobs.remove(job)
            job.update(status="failed", error=str(error), finished_at=time.time())
            store.save_job(job)
            errors.insert(0, (job, str(error)))
            raise
        return job, True


def probe_live(url):
    try:
        result = subprocess.run(
            [sys.executable, str(WORKER), "--probe", url],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            pass_fds=(store.owner.fileno(),),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("The live check timed out after 60 seconds; no download was started.") from None
    try:
        event = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("The live check did not return a valid result.") from None
    if result.returncode or event.get("event") == "error":
        raise RuntimeError(event.get("error") or "The live check failed.")
    if event.get("event") != "probe":
        raise RuntimeError("The live check did not return live status.")
    return event.get("live") is True


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if value is not None else None


@app.template_filter("download_progress")
def download_progress(job):
    if job["status"] != "complete" or not job.get("live"):
        return job["progress"]
    duration = job.get("duration")
    if duration is None:
        return "Recorded"
    minutes, seconds = divmod(int(duration), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        length = f"{hours}h {minutes}m"
    elif minutes:
        length = f"{minutes}m {seconds}s"
    else:
        length = f"{seconds}s" if duration >= 1 else "<1s"
    return f"Recorded - {length}"


def job_json(job):
    return {
        **{key: job.get(key) for key in ("id", "url", "title", "status", "live", "task_id", "error")},
        "progress": download_progress(job),
        "created_at": timestamp(job["created_at"]), "finished_at": timestamp(job.get("finished_at")),
        "can_stop": bool(job.get("stoppable") and not job.get("stopping") and job["status"] in ACTIVE_STATES),
        "status_url": f"/api/v1/downloads/{job['id']}",
        "download_url": f"/api/v1/downloads/{job['id']}/file" if job["status"] == "complete" else None,
    }


def task_json(task):
    return {key: timestamp(value) if key.endswith("_at") else value for key, value in task.items() if key != "revision"}


@app.before_request
def prepare_request():
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if (origin and urlsplit(origin).netloc != request.host) or request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403, "Cross-origin requests are not allowed")
    if request.endpoint != "static":
        init_runtime()


@app.after_request
def response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith("/api/"):
        response = error.get_response()
        response.data = app.json.dumps({"error": {"code": error.name.lower().replace(" ", "_"), "message": error.description}})
        response.content_type = "application/json"
        return response
    return error


@app.errorhandler(ValueError)
def validation_error(error):
    if request.path.startswith("/api/"):
        return jsonify(error={"code": "invalid_request", "message": str(error)}), 400
    return render_template("error.html", message=str(error)), 400


def download(job):
    process = None
    final_file = None
    failure = None
    path = None
    try:
        process = subprocess.Popen(
            [sys.executable, str(WORKER), job["url"], str(DOWNLOAD_DIR), job["id"]]
            + (["--live-only"] if job.get("live_only") else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            pass_fds=(store.owner.fileno(),),
        )
        with lock:
            job["worker"] = process
        for line in process.stdout:
            event = json.loads(line)
            kind = event.get("event")
            with lock:
                if kind == "metadata":
                    title = event.get("title") or "Untitled"
                    job["title"] = title[: TITLE_MAX - 1] + "…" if len(title) > TITLE_MAX else title
                    job["live"] = bool(event.get("live"))
                    if not job.get("stopping"):
                        job["status"] = "recording" if job["live"] else "downloading"
                    if job["live"] and not job.get("stopping"):
                        job["progress"] = "LIVE"
                elif kind == "progress" and not job.get("live") and not job.get("stopping"):
                    job["progress"] = event["progress"]
                elif kind == "recording":
                    job["stoppable"] = bool(event.get("stoppable"))
                elif kind == "stopping":
                    job["status"] = "stopping"
                    job["stopping"] = True
                    job["progress"] = "Stopping…"
                elif kind == "finalizing":
                    job["status"] = "finalizing"
                    job["stoppable"] = False
                    job["progress"] = "Finalizing…"
                elif kind == "complete":
                    final_file = event.get("file")
                    job["duration"] = event.get("duration")
                elif kind == "error":
                    failure = event.get("error") or "Download failed"
                if kind != "progress":
                    store.save_job(job)
        returncode = process.wait()
        if failure:
            raise RuntimeError(failure)
        if returncode:
            raise RuntimeError(f"Download worker exited with code {returncode}; partial files were retained")
        path = Path(final_file).resolve() if final_file else None
        if not path or not path.is_relative_to(Path(DOWNLOAD_DIR).resolve()) or not path.is_file():
            raise RuntimeError("Download ended without a completed file")
    except Exception as error:
        failure = str(error)
    finally:
        if process is not None:
            # EOF asks the isolated worker to finish if its monitor is interrupted.
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            process.wait()
        with lock:
            job.pop("worker", None)
            # Keep URL ownership until the worker has exited, including its
            # graceful cleanup after an event-pipe or parent-side failure.
            job.update(stoppable=False, finished_at=time.time())
            if failure:
                job.update(status="failed", error=failure)
            else:
                job.update(status="complete", file=str(path), progress="100%")
                job["progress"] = download_progress(job)
            store.save_job(job)
            jobs.remove(job)
            if failure:
                errors.insert(0, (job, failure))
            else:
                complete.insert(0, job)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if url:
            submit_download(url)
        return redirect("/")
    with lock:
        return render_template("index.html", jobs=jobs, complete=complete, errors=errors)


def stop_download(job_id):
    with lock:
        job = next((item for item in jobs if item["id"] == job_id), None)
        if job is None:
            if not any(item["id"] == job_id for item in complete) and not any(item[0]["id"] == job_id for item in errors):
                abort(404)
        elif not job.get("stopping"):
            if not job.get("live") or not job.get("stoppable"):
                abort(409, "This recording is not ready to stop or is already finalizing")
            process = job.get("worker")
            if process is not None and process.poll() is None:
                try:
                    process.stdin.write(json.dumps({"action": "stop"}) + "\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    # The worker may have finished between displaying and clicking Stop.
                    pass
                else:
                    job["stopping"] = True
                    job["status"] = "stopping"
                    job["progress"] = "Stopping…"
                    store.save_job(job)
        return find_job(job_id)


@app.post("/stop/<job_id>")
def stop(job_id):
    stop_download(job_id)
    if request.accept_mimetypes.best == "application/json":
        return "", 204
    return redirect("/", code=303)


@app.get("/status")
def status():
    with lock:
        return render_template("status.html", jobs=jobs, complete=complete, errors=errors)


@app.get("/download/<job_id>")
def serve(job_id, *, as_attachment=False):
    with lock:
        path = next((job.get("file") for job in complete if job["id"] == job_id), None)
    if not path or not Path(path).resolve().is_relative_to(Path(DOWNLOAD_DIR).resolve()) or not Path(path).is_file():
        abort(404)
    return send_file(path, as_attachment=as_attachment)


def json_object(allowed=None):
    data = request.get_json()
    if not isinstance(data, dict) or (allowed is not None and data.keys() - allowed):
        raise ValueError("Expected a JSON object" + (" with fields: " + ", ".join(sorted(allowed)) if allowed else ""))
    return data


@app.get("/api/v1/health")
def api_health():
    return jsonify(status="ok", api_version=1, scheduler_enabled=scheduler.thread is not None)


@app.route("/api/v1/downloads", methods=["GET", "POST"])
def api_downloads():
    if request.method == "POST":
        data = json_object({"url"})
        job, created = submit_download(data.get("url"))
        with lock:
            result = job_json(job)
        return jsonify(job=result, created=created), 202 if created else 200, {"Location": result["status_url"]}
    status_filter = request.args.get("status")
    if status_filter and status_filter not in (*ACTIVE_STATES, "complete", "failed", "interrupted", "active"):
        raise ValueError("Unknown download status")
    with lock:
        all_jobs = [*jobs, *complete, *(item[0] for item in errors)]
        all_jobs.sort(key=lambda job: job["created_at"], reverse=True)
        return jsonify(jobs=[job_json(job) for job in all_jobs if not status_filter or job["status"] == status_filter
                             or (status_filter == "active" and job["status"] in ACTIVE_STATES)])


@app.get("/api/v1/downloads/<job_id>")
def api_download(job_id):
    with lock:
        return jsonify(job=job_json(find_job(job_id)))


@app.post("/api/v1/downloads/<job_id>/stop")
def api_stop(job_id):
    with lock:
        job = stop_download(job_id)
        return jsonify(job=job_json(job)), 202 if job["status"] in ACTIVE_STATES else 200


@app.get("/api/v1/downloads/<job_id>/file")
def api_file(job_id):
    return serve(job_id, as_attachment=True)


def require_task(task_id):
    try:
        return scheduler.get(task_id)
    except KeyError:
        abort(404, "Task not found")


@app.route("/api/v1/tasks", methods=["GET", "POST"])
def api_tasks():
    if request.method == "POST":
        task = scheduler.save(json_object())
        return jsonify(task=task_json(task)), 201, {"Location": f"/api/v1/tasks/{task['id']}"}
    return jsonify(tasks=[task_json(task) for task in scheduler.list()])


@app.route("/api/v1/tasks/<task_id>", methods=["GET", "PATCH", "DELETE"])
def api_task(task_id):
    with lock:
        task = require_task(task_id)
        if request.method == "DELETE":
            scheduler.delete(task_id)
            return "", 204
        if request.method == "PATCH":
            task = scheduler.save(json_object(), task_id)
        return jsonify(task=task_json(task))


@app.post("/api/v1/tasks/<task_id>/run")
def api_run_task(task_id):
    with lock:
        require_task(task_id)
        started = scheduler.check(task_id)
        return jsonify(task=task_json(scheduler.get(task_id)), started=started), 202 if started else 200


def task_form():
    return {
        "name": request.form.get("name", ""),
        "url": request.form.get("url", ""),
        "cron": request.form.get("cron", ""),
        "timezone": request.form.get("timezone", ""),
        "mode": request.form.get("mode", ""),
        "enabled": "enabled" in request.form,
    }


@app.route("/tasks", methods=["GET", "POST"])
def tasks_page():
    if request.method == "POST":
        scheduler.save(task_form())
        return redirect("/tasks", code=303)
    tasks = [task_json(task) for task in scheduler.list()]
    return render_template(
        "tasks.html", tasks=tasks, checking=any(task["checking"] for task in tasks),
        new_task={"name": "", "url": "", "cron": "*/5 * * * *", "timezone": scheduler.default_timezone,
                  "mode": "live", "enabled": True},
    )


@app.post("/tasks/<task_id>/<action>")
def task_action(task_id, action):
    require_task(task_id)
    if action == "save":
        scheduler.save(task_form(), task_id)
    elif action == "run":
        scheduler.check(task_id)
    elif action == "delete":
        scheduler.delete(task_id)
    else:
        abort(404)
    return redirect("/tasks", code=303)


if __name__ == "__main__":
    try:
        init_runtime()
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error))
    app.run(host="0.0.0.0", port=8080, threaded=True)
