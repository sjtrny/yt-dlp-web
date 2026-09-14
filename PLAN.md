# Design

One Flask process serves the UI and API. Each download has a separate worker.
One scheduler runs persistent CRON Tasks.

## Files

| File | Purpose |
| --- | --- |
| `app.py` | HTTP routes and download control |
| `scheduler.py` | URL rules, CRON schedules, and Task checks |
| `state.py` | SQLite state and process lock |
| `recording_worker.py` | yt-dlp, FFmpeg Stop, and final file checks |
| `entrypoint.py` | Backend selection and startup checks |
| `templates/`, `static/` | HTML forms and CSS |
| `shortcuts/` | iOS Shortcut template, generator, and guide |
| `docs/` | API and Task guides |
| `tests/` | Local media, concurrency, restart, and browser checks |

## Downloads

1. Normalize the URL. Check the request key, then active downloads.
2. Return the existing job if either check finds one.
3. Store a new job and optional request key before starting its worker.
4. Read worker events to update the job state.
5. Keep the URL locked until the worker exits. This includes file finalization.
6. Mark the job complete only after a successful exit and final file check.

A shared lock and a SQLite unique index prevent active duplicate URLs.
Job IDs in output names prevent file conflicts. Each distinct active URL has
its own thread and process. There is no queue or worker pool.

## Live recordings

Stop sends a command to the selected worker. The worker tells its live FFmpeg
recorder to quit normally. It does not stop other jobs or postprocessing.
File checks and any MP4 conversion finish before the job becomes complete.

## Tasks

Each Task has at most one check in progress. Missed intervals produce one
check. Live mode checks metadata with a 60-second limit, then checks live status
again in the download worker. A Task edit cancels its current check before a
new download can start. An existing download continues.

Tasks, API requests, and HTML forms use the same download function.
See [Tasks](docs/tasks.md) for schedule and retry rules.

## Persistence

One process owns the state directory. Workers inherit its lock until they exit.
A replacement process cannot start while those workers still hold the lock.
On restart, unfinished jobs and checks become interrupted. Files, completed
links, request keys, and Tasks remain.

## UI

Tasks use HTML forms and no JavaScript. Downloads use a short status refresh
script. Stop uses a form. The UI has no help text.

See the [API guide](docs/api.md) for requests and errors.

## Backend

Use upstream yt-dlp by default. Select a complete custom package at startup
with a read-only mount. Keep private code outside this repository and image.
Use standard yt-dlp mounts for extractor plugins.

## Limits

No multi-user accounts, format controls, cookies UI, playlist UI, or job queue.
Run one app process with threaded request handling on port 8080.
