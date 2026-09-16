from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
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
stopped = []
errors = []
lock = RLock()
TITLE_MAX = 80
DOWNLOAD_DIR = Path(os.environ.get("YTDLP_DOWNLOAD_DIR", "/downloads"))
WORKER = Path(__file__).with_name("recording_worker.py")
store = None
scheduler = None
max_concurrent_downloads = 3


def download_limit():
    try:
        value = int(os.environ.get("YTDLP_MAX_CONCURRENT_DOWNLOADS") or "3")
        if value > 0:
            return value
    except ValueError:
        pass
    raise RuntimeError("YTDLP_MAX_CONCURRENT_DOWNLOADS must be a positive integer")


def init_runtime():
    global store, scheduler, max_concurrent_downloads
    with lock:
        if store is not None:
            return
        max_concurrent_downloads = download_limit()
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
                if job.get("kind") == "playlist":
                    job.update(discovery_done=True, resolved=True)
                candidate.save_job(job)
            if job["status"] == "complete":
                complete.append(job)
            elif job["status"] == "stopped":
                stopped.append(job)
            else:
                errors.append((job, job.get("error", "Download interrupted")))
        store = candidate
        scheduler = TaskScheduler(
            store, lock, submit_download, lambda url: active_download(url, claim=True), probe_live,
            default_timezone=os.environ.get("YTDLP_DEFAULT_TIMEZONE") or "UTC",
        )
        scheduler.start()


def active_download(url, *, claim=False):
    with lock:
        job = next((job for job in jobs if job["url"] == url), None)
        if job is not None and claim and not job.get("standalone", True):
            job["standalone"] = True
            store.save_job(job)
        return job


def find_job(job_id):
    with lock:
        job = next((item for item in [*jobs, *complete, *stopped, *(item[0] for item in errors)] if item["id"] == job_id), None)
        if job is None:
            abort(404, "Download not found")
        return job


def submit_download(url, *, task_id=None, live_only=False, playlist=None, title=None):
    url = normalize_url(url)
    with lock:
        existing = active_download(url, claim=playlist is None)
        if existing:
            return existing, False
        job = {"id": uuid4().hex, "url": url, "title": short_title(title or "Loading…"), "progress": "Queued",
               "kind": "video", "live": False, "stoppable": True, "stopping": False, "status": "queued",
               "created_at": time.time(), "finished_at": None, "task_id": task_id, "live_only": live_only,
               "resolved": bool(playlist or live_only), "standalone": playlist is None,
               "playlist_id": playlist["id"] if playlist else None}
        store.save_job(job)
        jobs.append(job)
        if playlist is None:
            dispatch_downloads()
        return job, True


def short_title(title):
    return title[: TITLE_MAX - 1] + "…" if len(title) > TITLE_MAX else title


def all_jobs():
    return [*jobs, *complete, *stopped, *(item[0] for item in errors)]


def finish_job(job, status, *, error=None, **fields):
    job.update(status=status, stoppable=False, stopping=False, finished_at=time.time(), **fields)
    if error:
        job["error"] = error
    store.save_job(job)
    jobs.remove(job)
    if status == "complete":
        complete.insert(0, job)
    elif status == "stopped":
        stopped.insert(0, job)
    else:
        errors.insert(0, (job, error or "Download interrupted"))


def dispatch_downloads():
    """Called under lock; reserve slots before starting any worker threads."""
    running = sum(job.get("kind") != "playlist" and job.get("resolved") and job["status"] != "queued" for job in jobs)
    discovering = sum(not job.get("resolved") and job["status"] != "queued" for job in jobs)
    for job in list(jobs):
        if job["status"] != "queued":
            continue
        discovery = not job.get("resolved")
        if (discovering if discovery else running) >= max_concurrent_downloads:
            continue
        job.update(status="discovering" if discovery else "starting",
                   progress="Loading…" if discovery else "Starting…", stoppable=False)
        store.save_job(job)
        try:
            Thread(target=download, args=(job,), daemon=True, name=f"download-{job['id'][:8]}").start()
        except Exception as error:
            finish_job(job, "failed", error=str(error))
        else:
            running += not discovery
            discovering += discovery


def playlist_counts(playlist, lookup=None):
    lookup = lookup if lookup is not None else {job["id"]: job for job in all_jobs()}
    counts = dict(total=len(playlist.get("entries", [])), complete=0, active=0, queued=0, stopped=0, failed=0)
    for entry in playlist.get("entries", []):
        state = "stopped" if entry.get("cancelled") else lookup[entry["id"]]["status"]
        key = "active" if state in ACTIVE_STATES and state != "queued" else state
        counts[key if key in counts else "failed"] += 1
    return counts


def refresh_playlists():
    lookup = {job["id"]: job for job in all_jobs()}
    for playlist in list(jobs):
        if playlist.get("kind") != "playlist":
            continue
        counts = playlist_counts(playlist, lookup)
        playlist["progress"] = f'{counts["complete"]} of {counts["total"]} complete'
        if not playlist.get("discovery_done") or counts["active"] or counts["queued"]:
            continue
        if playlist.get("stopping"):
            finish_job(playlist, "stopped", progress="Stopped · " + playlist["progress"])
        elif playlist.get("error") or counts["failed"]:
            finish_job(playlist, "failed", error=playlist.get("error") or f'{counts["failed"]} playlist videos failed')
        elif counts["stopped"]:
            finish_job(playlist, "stopped", progress="Stopped · " + playlist["progress"])
        else:
            finish_job(playlist, "complete")


def add_playlist_entry(playlist, event):
    if playlist.get("stopping"):
        return
    title = short_title(event.get("title") or "Unavailable video")
    try:
        if event.get("error"):
            raise ValueError(event["error"])
        url = normalize_url(event.get("url"))
        if url == playlist["url"]:
            raise ValueError("Playlist entry points back to the playlist")
        # Duplicate entries within a playlist use one download, even if it has
        # already finished while the remaining entries are being discovered.
        lookup = {job["id"]: job for job in all_jobs()}
        if any(lookup[entry["id"]]["url"] == url for entry in playlist["entries"]):
            return
        child, _ = submit_download(url, task_id=playlist.get("task_id"), playlist=playlist, title=title)
        if child.get("kind") == "playlist":
            raise ValueError("This entry refers to another active playlist, not a video")
    except ValueError as error:
        child = {"id": uuid4().hex, "url": playlist["url"], "title": title, "kind": "video",
                 "status": "failed", "progress": "Failed", "error": str(error), "live": False,
                 "created_at": time.time(), "finished_at": time.time(), "playlist_id": playlist["id"],
                 "task_id": playlist.get("task_id")}
        store.save_job(child)
        errors.insert(0, (child, str(error)))
    playlist["entries"].append({"id": child["id"]})
    store.save_job(playlist)
    dispatch_downloads()


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
    if job.get("kind") == "playlist":
        counts = playlist_counts(job)
        return ("Stopped · " if job["status"] == "stopped" else "") + f'{counts["complete"]} of {counts["total"]} complete'
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
        "kind": job.get("kind", "video"), "playlist_id": job.get("playlist_id"),
        **({"entries": job["entries"], "counts": playlist_counts(job),
            "discovery_done": bool(job.get("discovery_done"))} if job.get("kind") == "playlist" else {}),
        "progress": download_progress(job),
        "created_at": timestamp(job["created_at"]), "finished_at": timestamp(job.get("finished_at")),
        "can_stop": bool(job.get("stoppable") and not job.get("stopping") and job["status"] in ACTIVE_STATES),
        "status_url": f"/api/v1/downloads/{job['id']}",
        "download_url": f"/api/v1/downloads/{job['id']}/file" if job["status"] == "complete" and job.get("kind") != "playlist" else None,
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
    was_stopped = False
    discovery = not job.get("resolved")
    metadata_received = False
    playlist_received = False
    try:
        process = subprocess.Popen(
            [sys.executable, str(WORKER), job["url"], str(DOWNLOAD_DIR), job["id"]]
            + (["--discover"] if discovery else ["--live-only"] if job.get("live_only") else ["--single-video"]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            pass_fds=(store.owner.fileno(),),
            start_new_session=True,
        )
        with lock:
            job["worker"] = process
            if job.get("stopping"):
                Thread(target=cancel_transfer, args=(process,), daemon=True).start()
        for line in process.stdout:
            event = json.loads(line)
            kind = event.get("event")
            with lock:
                if kind == "playlist":
                    job.update(kind="playlist", title=short_title(event.get("title") or "Playlist"),
                               entries=[], discovery_done=False, stoppable=True)
                    if not job.get("stopping"):
                        job.update(status="discovering", progress="Loading playlist…")
                    dispatch_downloads()
                elif kind == "entry" and job.get("kind") == "playlist":
                    add_playlist_entry(job, event)
                elif kind == "playlist_complete":
                    playlist_received = True
                elif kind == "metadata":
                    metadata_received = True
                    title = event.get("title") or "Untitled"
                    job["title"] = short_title(title)
                    job["live"] = bool(event.get("live"))
                    job["stoppable"] = not job["live"] and not discovery
                    if not discovery and not job.get("stopping"):
                        job["status"] = "recording" if job["live"] else "downloading"
                        job["progress"] = "LIVE" if job["live"] else "0%"
                    dispatch_downloads()
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
                elif kind == "stopped":
                    was_stopped = True
                elif kind == "error":
                    failure = event.get("error") or "Download failed"
                if kind not in ("progress", "entry"):
                    store.save_job(job)
        returncode = process.wait()
        cancelled = job.get("transfer_cancelled") or (job.get("stopping") and not job.get("live"))
        # A published, verified file wins a Stop/completion race. Otherwise a
        # cancelled transfer is not a failed or completed download.
        was_stopped = (was_stopped or cancelled) and not final_file
        if was_stopped:
            failure = None
        elif failure:
            raise RuntimeError(failure)
        elif returncode and not (cancelled and final_file):
            raise RuntimeError(f"Download worker exited with code {returncode}; partial files were retained")
        if discovery and not was_stopped:
            if job.get("kind") == "playlist" and not playlist_received:
                raise RuntimeError("Playlist discovery ended before the listing was complete")
            if job.get("kind") != "playlist" and not metadata_received:
                raise RuntimeError("Could not read video metadata")
        path = Path(final_file).resolve() if final_file else None
        if not discovery and not was_stopped and (not path or not path.is_relative_to(Path(DOWNLOAD_DIR).resolve()) or not path.is_file()):
            raise RuntimeError("Download ended without a completed file")
    except Exception as error:
        failure = str(error)
    finally:
        if process is not None:
            if failure and (discovery or not job.get("live")):
                cancel_transfer(process)
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
            if job.get("kind") == "playlist":
                job.update(discovery_done=True, resolved=True)
                if not job.get("stopping"):
                    job["status"] = "downloading"
                if failure:
                    job["error"] = failure
                elif not job["entries"] and not job.get("stopping"):
                    job["error"] = "Playlist contains no videos"
                store.save_job(job)
            elif failure:
                finish_job(job, "failed", error=failure)
            elif was_stopped:
                finish_job(job, "stopped", progress="Stopped")
            elif discovery:
                job.update(resolved=True, status="queued", progress="Queued", stoppable=True)
                store.save_job(job)
            else:
                job.update(status="complete", progress="100%")
                finish_job(job, "complete", file=str(path), progress=download_progress(job))
            dispatch_downloads()
            refresh_playlists()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if url:
            submit_download(url)
        return redirect("/")
    with lock:
        return render_template("index.html", **status_context())


def cancel_transfer(process):
    """Stop only this download's isolated process group, including FFmpeg."""
    try:
        # The request saves its stopping state under this lock before a fast
        # worker exit can be mistaken for a failed transfer.
        with lock:
            if process.poll() is not None:
                return
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Some backends wait for blocked fragment threads during cleanup.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    except ProcessLookupError:
        pass  # Natural completion can race with the Stop request.


def request_stop(job, *, force=False):
    if job.get("stopping") or job["status"] not in ACTIVE_STATES:
        return
    if job["status"] == "queued":
        finish_job(job, "stopped", progress="Cancelled")
        return
    if not job.get("stoppable"):
        if not force:
            abort(409, "This download is not ready to stop or is already finalizing")
        if job["status"] == "finalizing":
            return
    job.update(stopping=True, status="stopping", progress="Stopping…")
    job["transfer_cancelled"] = not job.get("live") or not job.get("resolved")
    store.save_job(job)
    process = job.get("worker")
    if process is not None and process.poll() is None:
        try:
            if job.get("live") and job.get("resolved"):
                process.stdin.write(json.dumps({"action": "stop"}) + "\n")
                process.stdin.flush()
            else:
                Thread(target=cancel_transfer, args=(process,), daemon=True,
                       name=f"stop-{job['id'][:8]}").start()
        except (BrokenPipeError, OSError, ValueError):
            pass  # Natural completion can race with Stop.


def stop_download(job_id):
    with lock:
        job = next((item for item in jobs if item["id"] == job_id), None)
        if job is None:
            return find_job(job_id)
        request_stop(job)
        if job.get("kind") == "playlist":
            lookup = {item["id"]: item for item in all_jobs()}
            needed_elsewhere = {
                entry["id"] for other in jobs
                if other is not job and other.get("kind") == "playlist" and not other.get("stopping")
                for entry in other["entries"] if not entry.get("cancelled")
            }
            for entry in job["entries"]:
                child = lookup[entry["id"]]
                if child["status"] not in ACTIVE_STATES:
                    continue
                shared = child.get("standalone", True) or child["id"] in needed_elsewhere
                if shared:
                    entry["cancelled"] = True
                else:
                    request_stop(child, force=True)
            store.save_job(job)
        dispatch_downloads()
        refresh_playlists()
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
        return render_template("status.html", **status_context())


def status_context():
    lookup = {job["id"]: job for job in all_jobs()}
    playlists = []
    grouped = set()
    for job in jobs:
        if job.get("kind") != "playlist":
            continue
        members = [lookup[entry["id"]] for entry in job["entries"] if not entry.get("cancelled")]
        grouped.update(member["id"] for member in members)
        playlists.append(dict(job=job, counts=playlist_counts(job, lookup),
                              active=[member for member in members if member["status"] in ACTIVE_STATES and member["status"] != "queued"],
                              queued=[member for member in members if member["status"] == "queued"]))
    ungrouped = [job for job in jobs if job.get("kind") != "playlist" and job["id"] not in grouped]
    return dict(jobs=[job for job in ungrouped if job["status"] != "queued"],
                queued=[job for job in ungrouped if job["status"] == "queued"], playlists=playlists,
                complete=[job for job in complete if job.get("kind") != "playlist"],
                stopped=stopped, errors=errors)


@app.post("/completed/clear")
@app.post("/completed/<job_id>/remove")
def clear_completed(job_id=None):
    with lock:
        if job_id is not None:
            job = find_job(job_id)
            if job["status"] != "complete":
                abort(409, "Only completed downloads can be removed from this list")
            selected = [job]
        else:
            selected = [job for job in complete if not job.get("hidden")]
        store.hide_completed(selected)
        for job in selected:
            job["hidden"] = True
    return redirect("/", code=303)


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
    if status_filter and status_filter not in (*ACTIVE_STATES, "complete", "stopped", "failed", "interrupted", "active"):
        raise ValueError("Unknown download status")
    with lock:
        ordered = sorted(all_jobs(), key=lambda job: job["created_at"], reverse=True)
        return jsonify(jobs=[job_json(job) for job in ordered if not status_filter or job["status"] == status_filter
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
        port = int(os.environ.get("YTDLP_PORT") or "8080")
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("YTDLP_PORT must be an integer from 1 to 65535") from None
    try:
        init_runtime()
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error))
    app.run(host="0.0.0.0", port=port, threaded=True)
