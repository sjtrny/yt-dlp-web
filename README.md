# yt-dlp-web

Web UI for yt-dlp.

![Browser window showing active and completed downloads with Stop, X, and Clear all controls](docs/images/download-in-progress.png)

## Run

```sh
docker run -p 127.0.0.1:8080:8080 -v ./downloads:/downloads ghcr.io/sjtrny/yt-dlp-web
```

Open <http://localhost:8080>.

## Downloads

Paste a video, live-stream, or playlist URL and select **Download**.
Playlists show a group with completed, active, and queued counts. Videos enter
the queue as they are found. Expand the queued list to see the remaining videos.
Select a title to open its source. For a finished video, select its status
(**100%** or **Recorded**) to open the downloaded file.

**Cancel** removes a queued video from the queue. **Stop playlist** stops its
downloads and cancels its queued videos. A video shared with another active
playlist or a separate request continues. Completed and partial files are kept.
**X** and **Clear all** hide completed, stopped, or failed entries without
deleting full or partial files.

## Documentation

- [Tasks](docs/tasks.md)
- [API](docs/api.md)

## Configuration

| Variable | Default | Use |
| --- | --- | --- |
| `YTDLP_PORT` | `8080` | Web listening port (1–65535); unset or empty uses the default |
| `YTDLP_MAX_CONCURRENT_DOWNLOADS` | `3` | Maximum active non-Task downloads; positive integer; unset or empty uses the default |
| `YTDLP_DOWNLOAD_DIR` | `/downloads` | Media |
| `YTDLP_STATE_DIR` | `<download-dir>/.yt-dlp-web` | History and Tasks |
| `YTDLP_DEFAULT_TIMEZONE` | `UTC` | Default TZ for new Tasks; IANA time zone |
| `YTDLP_OVERRIDE_DIR` | Unset | Custom yt-dlp package |

For port 9090, use `-e YTDLP_PORT=9090 -p 127.0.0.1:9090:9090` in place of
the default `-p` option. Open <http://localhost:9090>.

For two active downloads, add `-e YTDLP_MAX_CONCURRENT_DOWNLOADS=2` to the
Docker command. Videos, playlist entries, and live recordings from manual
requests share this limit. A slot stays occupied through file finalization.
Downloads started by Tasks do not use or wait for these slots. Playlist
discovery does not use a download slot.

## State and restart

History and Tasks stay in the state directory. On restart, queued and unfinished
jobs become interrupted; they do not restart automatically. Submit the URL
again to download it. Existing files and playback links remain available.

## Backend

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
