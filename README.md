# yt-dlp-web

A small Flask app for yt-dlp. Download videos, record live streams, and run
scheduled Tasks. The UI uses HTML forms and a short status refresh script.

| Guide | Contents |
| --- | --- |
| [HTTP API](docs/api.md) | Routes, responses, and retries |
| [iOS Shortcut](shortcuts/README.md) | Safari sharing, setup, and template |
| [Tasks](docs/tasks.md) | Controls, CRON schedules, and duplicate rules |
| [Design](PLAN.md) | Files and runtime behavior |

## Build and run

```sh
docker build -t yt-dlp-web:local .
mkdir -p downloads
docker run --name yt-dlp-web --rm -p 127.0.0.1:8080:8080 \
  --mount "type=bind,src=$(pwd)/downloads,dst=/downloads" \
  yt-dlp-web:local
```

Open <http://localhost:8080>. The image includes upstream yt-dlp and FFmpeg.
Files, job history, Tasks, and request keys stay in the mounted directory.
Finish active recordings before you restart the app.

## Configuration

| Variable | Default | Use |
| --- | --- | --- |
| `YTDLP_DOWNLOAD_DIR` | `/downloads` | Existing writable media directory |
| `YTDLP_STATE_DIR` | `<download-dir>/.yt-dlp-web` | Persistent state directory |
| `YTDLP_SCHEDULER_ENABLED` | `1` | Set to `0` to stop automatic checks |
| `YTDLP_OVERRIDE_DIR` | Unset | Complete custom yt-dlp package directory |

Set variables with Docker `--env` or in the local process environment.
Mount media and state on persistent storage.

## Downloads and Stop

Enter a URL and select **Download**. Select a completed title to get its file.
The API and Tasks use the same download service.

Only one download per normalized URL can be active. Each distinct URL gets a
worker. There is no queue or application limit on concurrent downloads.

Select **Stop** to end a live FFmpeg recording. Wait for **Complete** before
you use the file or restart the app. Other downloads continue. Repeated Stop
requests are safe. Ordinary downloads do not have a Stop control.

FFmpeg closes the recording normally. The worker checks the file and converts
live MPEG-TS data to MP4 when needed, without re-encoding. It keeps the original
file until the new file passes the check. This step needs temporary disk space.

## State and restart

State is stored in `state.sqlite3`. Run one app process per state directory.
Use local storage with SQLite and file-lock support. Keep `owner.lock` in place
while the app or its workers run. A second process cannot use the same state.
Workers keep the lock until they exit, even if the app has stopped.

After restart, unfinished jobs become `interrupted`. Partial files remain.
The app does not resume them. Completed links, request keys, and Tasks remain.
Old files with no stored job record do not get new download links.

The default entrypoint starts the scheduler before serving requests. A custom
server must call `app.init_runtime()` in one process before serving requests.
Do not use a multi-process server. Manual Task runs remain available when
automatic checks are disabled.

## Custom yt-dlp package

1. Install the custom wheel into a new directory outside this repository.
   Include its dependencies and `default,curl-cffi,deno` extras. Use the same
   Python version, operating system, and architecture as the image.
2. Include the full `yt_dlp/` package and its `yt_dlp-*.dist-info/` metadata.
   A patched source file or standalone executable is not sufficient.
3. Mount the directory read-only and set `YTDLP_OVERRIDE_DIR`:

```sh
docker run --name yt-dlp-web --rm -p 127.0.0.1:8080:8080 \
  --env YTDLP_OVERRIDE_DIR=/opt/yt-dlp-override \
  --mount type=bind,src=/absolute/custom-bundle,dst=/opt/yt-dlp-override,readonly \
  --mount type=bind,src=/absolute/downloads,dst=/downloads \
  yt-dlp-web:local
```

Mount sources must exist on the Docker host. The container user must be able
to write to the downloads directory. Startup fails if it cannot write there.

The entrypoint sets `PYTHONPATH` and adds the bundle's `bin/` to `PATH`.
It checks the package and metadata before startup. An invalid or empty override
fails startup. Logs show the selected package and version.

Keep executable paths valid at the final mount location. Check native tools
against the image. FFmpeg stays in the image.

Use a separate bundle for each version. To update, finish active recordings
and recreate the container with the new bundle. To return to a previous version,
use its bundle and compatible image. Unset `YTDLP_OVERRIDE_DIR` to use upstream
yt-dlp. Do not change a bundle while it is in use.

The custom package must support the yt-dlp Python API. Stop also needs a live
`FFmpegFD` recorder with interactive input. Check Stop after a backend update.
Keep private packages and build records outside this repository and its image.

## Extractor plugins

Use the standard yt-dlp plugin layout outside this repository:

```text
plugins/example-package/yt_dlp_plugins/extractor/example.py
```

Add this mount to the run command, then restart the app:

```text
--mount type=bind,src=/absolute/plugins,dst=/etc/yt-dlp/plugins,readonly
```

The first `YoutubeDL` instance loads the plugins. The app has no custom plugin
API or postprocessor plugin controls.

## Local development

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p downloads
YTDLP_DOWNLOAD_DIR="$PWD/downloads" .venv/bin/python entrypoint.py
```

Use the entrypoint to check and select the backend. Start another Python server
with `.venv/bin/python entrypoint.py <command>`. Direct use of `app.py` skips
the backend checks.

Run the Python checks with FFmpeg available:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Run the browser check with Playwright and Chromium available:

```sh
node --test tests/tasks_ui.cjs
```

The browser check uses `.venv/bin/python`, or `PYTHON` if set. The checks use
local media and temporary state. They do not change existing downloads.

Check the image and system plugin mount without external network access:

```sh
docker run --rm --network none --read-only --tmpfs /tmp:exec \
  --env YTDLP_TEST_SYSTEM_PLUGINS=1 \
  --mount "type=bind,src=$(pwd)/tests,dst=/app/tests,readonly" \
  --mount "type=bind,src=$(pwd)/tests/fixtures/yt-dlp/plugins,dst=/etc/yt-dlp/plugins,readonly" \
  yt-dlp-web:local python -m unittest discover -s tests -v
```

The image includes only application files. It does not include tests, media,
or custom packages.
