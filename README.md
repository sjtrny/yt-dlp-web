# yt-dlp-web

Download videos and record live streams with yt-dlp from a web browser. Files
stay on the server.

- Download a URL now.
- Stop a live recording and keep the recorded video.
- Check a URL on a CRON schedule.
- Control downloads and Tasks through an HTTP API.
- Send the current Safari page from an iPhone.

| Guide | Use |
| --- | --- |
| [HTTP API](docs/api.md) | Start and manage downloads and Tasks |
| [iOS Shortcut](shortcuts/README.md) | Send a Safari URL to the server |
| [Tasks](docs/tasks.md) | Set automatic URL checks |

## Start

Build and run the Docker image:

```sh
docker build -t yt-dlp-web:local .
mkdir -p downloads
docker run --name yt-dlp-web --rm -p 127.0.0.1:8080:8080 \
  --mount "type=bind,src=$(pwd)/downloads,dst=/downloads" \
  yt-dlp-web:local
```

Open <http://localhost:8080>. The image includes yt-dlp and FFmpeg. The mounted
`downloads` directory stores media, download history, and Tasks.

## Download

Enter a video or stream URL, then select **Download**. The Downloads page shows
the current status. Select a completed title to get its file.

Only one download can be active for the same URL. You can download different
URLs at the same time.

Select **Stop** to end a live recording. Wait for **Complete** before you use
the file. Other downloads continue.

## Tasks

A Task checks one URL on a CRON schedule. Open **Tasks** to add or change one.

- **Live** starts a download only when the URL has a live stream.
- **Always** starts a download each time the schedule runs.
- **Enabled** starts or pauses automatic checks.
- **Run** checks the URL now.

The scheduler runs while the app runs. A check does not start a second download
when the same URL is already active. See the [Tasks guide](docs/tasks.md) for
CRON examples and all controls.

Pause a Task before you stop its live recording. This prevents the next check
from starting it again.

## iPhone

Use the iOS Shortcut to send the current Safari page to the server. The server
downloads the video. The file stays on the server. See the
[iOS Shortcut guide](shortcuts/README.md).

## HTTP API

Use the API to start downloads, read status, stop live recordings, and manage
Tasks. See the [HTTP API guide](docs/api.md).

## Storage and restart

Keep the downloads directory on persistent storage. It contains media, history,
and Tasks.

After a restart, completed downloads and Tasks remain. An unfinished download
becomes **Interrupted** and does not resume. Its partial file remains. Finish
active recordings before you restart the app.

Use only one app instance for each state directory.

## Configuration

| Variable | Default | Use |
| --- | --- | --- |
| `YTDLP_DOWNLOAD_DIR` | `/downloads` | Media directory |
| `YTDLP_STATE_DIR` | `<download-dir>/.yt-dlp-web` | History and Tasks directory |
| `YTDLP_OVERRIDE_DIR` | Unset | Optional custom yt-dlp package |

Pass variables to Docker with `--env NAME=value`.

## Optional custom yt-dlp

The image uses the standard yt-dlp package. You can use a complete custom
package from a directory outside this repository. Mount the directory as
read-only and set `YTDLP_OVERRIDE_DIR` to its container path:

```text
--env YTDLP_OVERRIDE_DIR=/opt/yt-dlp-override
--mount type=bind,src=/absolute/custom-package,dst=/opt/yt-dlp-override,readonly
```

The package must match the image platform and Python version. Restart the app
after you change the package. Unset `YTDLP_OVERRIDE_DIR` to use standard yt-dlp.

## Optional extractor plugins

Mount a standard yt-dlp plugin directory as read-only, then restart the app:

```text
--mount type=bind,src=/absolute/plugins,dst=/etc/yt-dlp/plugins,readonly
```
