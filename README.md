# yt-dlp-web

Web UI for yt-dlp.

![Browser window showing active and completed downloads with Stop, X, and Clear all controls](docs/images/download-in-progress.png)

## Run

```sh
docker run -p 127.0.0.1:8080:8080 -v ./downloads:/downloads ghcr.io/sjtrny/yt-dlp-web
```

Open <http://localhost:8080>.

## Documentation

- [Tasks](docs/tasks.md)
- [API](docs/api.md)
- [iOS Shortcut](shortcuts/README.md)

## Configuration

| Variable | Default | Use |
| --- | --- | --- |
| `YTDLP_PORT` | `8080` | Web listening port (1–65535); unset or empty uses the default |
| `YTDLP_DOWNLOAD_DIR` | `/downloads` | Media |
| `YTDLP_STATE_DIR` | `<download-dir>/.yt-dlp-web` | History and Tasks |
| `YTDLP_DEFAULT_TIMEZONE` | `UTC` | Default TZ for new Tasks; IANA time zone |
| `YTDLP_OVERRIDE_DIR` | Unset | Custom yt-dlp package |

For port 9090, use `-e YTDLP_PORT=9090 -p 127.0.0.1:9090:9090` in place of
the default `-p` option. Open <http://localhost:9090>.

### Overrides

Custom yt-dlp package:

```text
--env YTDLP_OVERRIDE_DIR=/opt/yt-dlp-override
--mount type=bind,src=/absolute/custom-package,dst=/opt/yt-dlp-override,readonly
```

Extractor plugins:

```text
--mount type=bind,src=/absolute/plugins,dst=/etc/yt-dlp/plugins,readonly
```
