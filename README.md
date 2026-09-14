# yt-dlp-web

Web UI, HTTP API, CRON Tasks, and iOS Shortcut for yt-dlp.

[API](docs/api.md) · [iOS Shortcut](shortcuts/README.md) · [Tasks](docs/tasks.md)

## Run

```sh
docker build -t yt-dlp-web:local .
mkdir -p downloads
docker run --name yt-dlp-web --rm -p 127.0.0.1:8080:8080 \
  --mount "type=bind,src=$(pwd)/downloads,dst=/downloads" \
  yt-dlp-web:local
```

Open <http://localhost:8080>.

## Behavior

- One active download per URL.
- **Stop** finalizes a live recording. Wait for **Complete**.
- A **Live** Task starts only when the stream is live.
- An **Always** Task starts at each scheduled time.
- The scheduler is always enabled.
- Pause a Task before stopping its recording to prevent a scheduled restart.

## Restart

The downloads mount stores media, history, and Tasks. Unfinished downloads
become **Interrupted** after restart and do not resume. Partial files remain.

Use one app instance per state directory.

## Configuration

| Variable | Default | Use |
| --- | --- | --- |
| `YTDLP_DOWNLOAD_DIR` | `/downloads` | Media |
| `YTDLP_STATE_DIR` | `<download-dir>/.yt-dlp-web` | History and Tasks |
| `YTDLP_OVERRIDE_DIR` | Unset | Custom yt-dlp package |

## Overrides

Custom yt-dlp package:

```text
--env YTDLP_OVERRIDE_DIR=/opt/yt-dlp-override
--mount type=bind,src=/absolute/custom-package,dst=/opt/yt-dlp-override,readonly
```

Extractor plugins:

```text
--mount type=bind,src=/absolute/plugins,dst=/etc/yt-dlp/plugins,readonly
```
