"""Deterministic process barriers for API/scheduler concurrency checks only.

Real yt-dlp, HLS and FFmpeg are exercised separately by the runtime suite.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)


barriers = Path(os.environ["YTDLP_TEST_BARRIER_DIR"])
if sys.argv[1] == "--probe":
    url = sys.argv[2]
    if "delayed" in url:
        (barriers / "probe-arrived").touch()
        while not (barriers / "release-probe").exists():
            time.sleep(.01)
    if "probe-error" in url:
        emit("error", error="Fixture extractor could not check this page")
        sys.exit(1)
    emit("probe", live="offline" not in url and "upcoming" not in url)
else:
    url, directory, job_id = sys.argv[1:4]
    if "/playlist" in url:
        emit("playlist", title="Controlled playlist")
        for index in range(1, 7):
            emit("entry", title=f"Video {index}", url=f"https://example.test/video-entry-{index}")
            if index == 2 and "slow" in url:
                (barriers / "discovery-arrived").touch()
                while not (barriers / "release-discovery").exists():
                    time.sleep(.01)
            if index == 2 and "broken" in url:
                emit("error", error="Playlist listing failed")
                sys.exit(1)
        if "mixed" in url:
            emit("entry", title="Duplicate", url="https://example.test/video-entry-1")
            emit("entry", title="Unavailable video", error="Private video")
        emit("playlist_complete")
        sys.exit(0)
    ordinary = "/video" in url
    if "/video-blocked" in url:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    entry = urlsplit(url).path.removeprefix("/") if "/video-entry-" in url else None
    emit("metadata", title=f"Video {entry.rsplit('-', 1)[1]}" if entry else "Controlled video" if ordinary else "Controlled recording", live=not ordinary)
    if "--discover" in sys.argv[4:]:
        sys.exit(0)
    emit("recording", stoppable=True)
    if entry:
        (barriers / f"started-{entry}").touch()
        (Path(directory) / f"{job_id}.part").write_bytes(b"partial media")
        emit("progress", progress="42.0%")
        while not (barriers / f"release-{entry}").exists():
            time.sleep(.01)
    else:
        for line in sys.stdin:
            if json.loads(line).get("action") == "stop":
                break
        emit("stopping")
    emit("finalizing")
    while not entry and not (barriers / "release-finalizing").exists():
        time.sleep(.01)
    output = Path(directory) / f"{job_id}.mp4"
    media = os.environ.get("YTDLP_TEST_MEDIA_FILE")
    if media:
        shutil.copyfile(media, output)
        duration = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(output)], text=True,
        ))
    else:
        output.write_bytes(b"controlled test output; not actual media")
        duration = 4980
    emit("complete", file=str(output), duration=duration)
