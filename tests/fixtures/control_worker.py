"""Deterministic process barriers for API/scheduler concurrency checks only.

Real yt-dlp, HLS and FFmpeg are exercised separately by the runtime suite.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


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
    emit("metadata", title="Controlled recording", live=True)
    emit("recording", stoppable=True)
    for line in sys.stdin:
        if json.loads(line).get("action") == "stop":
            break
    emit("stopping")
    emit("finalizing")
    while not (barriers / "release-finalizing").exists():
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
