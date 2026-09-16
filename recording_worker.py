"""Run one yt-dlp job with isolated download and live-recording Stop controls.

The parent reads newline-delimited JSON from stdout and writes ``{"action":
"stop"}`` to stdin for live recordings. SIGTERM cancels ordinary downloads.
Backend output, including FFmpeg output, goes to stderr.
"""

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from threading import Lock, Thread, local


class DownloadStopped(BaseException):
    """Leave downloader retry handlers and unwind open files/subprocesses."""


def cancel_transfer(signum, frame):
    raise DownloadStopped


class Events:
    def __init__(self, stream):
        self.stream = stream
        self.lock = Lock()
        self.closed = False

    def send(self, event, **data):
        with self.lock:
            if self.closed:
                return
            try:
                self.stream.write(json.dumps({"event": event, **data}) + "\n")
                self.stream.flush()
            except (BrokenPipeError, OSError):
                # Losing the parent must not interrupt FFmpeg finalization.
                # The command reader also sees EOF and requests a safe stop.
                self.closed = True


class RecordingControl:
    def __init__(self, events):
        self.events = events
        self.lock = Lock()
        self.recorder = None
        self.stop_requested = False
        self.quit_sent = False
        self.finalizing = False
        self.finished = False

    def request_stop(self):
        with self.lock:
            if self.finished or self.finalizing:
                return
            self.stop_requested = True
            self._quit_locked()

    def attach(self, proc):
        with self.lock:
            if self.finished:
                return
            self.recorder = proc
            self.quit_sent = False
            self.finalizing = False
            self.events.send("recording", stoppable=True)
            self._quit_locked()

    def detach(self, proc):
        with self.lock:
            if self.recorder is not proc:
                return
            self.recorder = None
            self.finalizing = True
            self.events.send("finalizing")

    def finish(self):
        with self.lock:
            self.finished = True

    def _quit_locked(self):
        proc = self.recorder
        if not self.stop_requested or self.quit_sent or proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            # FFmpeg's interactive quit closes its muxer and writes trailers.
            # Do not signal/kill the process or close its input prematurely.
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            # Natural completion can race with a Stop request. Its normal
            # return code and postprocessing still determine the result.
            return
        self.quit_sent = True
        self.events.send("stopping")


def read_commands(stream, control):
    pending = b""
    try:
        # Do not block a daemon thread in Python's buffered stdin reader:
        # interpreter shutdown may otherwise wait on that reader's lock.
        while data := os.read(stream.fileno(), 4096):
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    command = json.loads(line)
                except (ValueError, TypeError, UnicodeDecodeError):
                    continue
                if isinstance(command, dict) and command.get("action") == "stop":
                    control.request_stop()
    except OSError:
        pass
    finally:
        control.request_stop()


@contextmanager
def controlled_ffmpeg(control):
    """Adapt only this worker's actual live FFmpeg downloader processes.

    yt-dlp does not expose a recorder-process hook for URL inputs. This
    adapter is therefore deliberately isolated in one worker interpreter.
    The download-call context and output argument exclude executable probes,
    other external downloaders, piped media input, and all postprocessors.
    """
    from yt_dlp.downloader import external
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    original_popen = external.Popen
    original_call = external.FFmpegFD._call_downloader
    context = local()

    class RecorderPopen(original_popen):
        def __init__(self, args, *remaining, **kwargs):
            self.controlled_recording = False
            super().__init__(args, *remaining, **kwargs)
            output = getattr(context, "output", None)
            if (output is not None and isinstance(args, (list, tuple))
                    and args and args[-1] == output
                    and "-nostdin" not in args
                    and kwargs.get("stdin") == subprocess.PIPE):
                self.controlled_recording = True
                control.attach(self)

        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                if self.controlled_recording:
                    control.detach(self)

    def call_downloader(downloader, filename, info, *args, **kwargs):
        previous = getattr(context, "output", None)
        formats = info.get("requested_formats") or [info]
        live = info.get("is_live") or info.get("live_status") == "is_live"
        piped = any(item.get("url") in ("-", "pipe:") for item in formats)
        context.output = (
            FFmpegPostProcessor._ffmpeg_filename_argument(filename)
            if live and not piped and filename != "-" else None
        )
        try:
            return original_call(downloader, filename, info, *args, **kwargs)
        finally:
            context.output = previous

    external.Popen = RecorderPopen
    external.FFmpegFD._call_downloader = call_downloader
    try:
        yield
    finally:
        external.FFmpegFD._call_downloader = original_call
        external.Popen = original_popen


def probe_media(path):
    """Require an actual audio/video stream with media packets, not a header."""
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("No media was recorded. The recording may have stopped before any media arrived.")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-read_intervals", "%+1",
         "-show_entries",
         "format=format_name,duration:stream=index,codec_type,codec_name,width,height,sample_rate,channels:packet=stream_index,size",
         "-of", "json", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=60,
    )
    try:
        info = json.loads(result.stdout)
    except (TypeError, ValueError):
        info = {}
    valid_streams = set()
    for stream in info.get("streams", []):
        if stream.get("codec_name") in (None, "unknown"):
            continue
        if stream.get("codec_type") == "video":
            valid = stream.get("width", 0) > 0 and stream.get("height", 0) > 0
        elif stream.get("codec_type") == "audio":
            valid = stream.get("channels", 0) > 0 and int(stream.get("sample_rate") or 0) > 0
        else:
            valid = False
        if valid:
            valid_streams.add(stream.get("index"))
    has_media = any(
        packet.get("stream_index") in valid_streams and int(packet.get("size") or 0) > 0
        for packet in info.get("packets", [])
    )
    if result.returncode or not valid_streams or not has_media:
        raise RuntimeError(
            "No playable audio or video was recorded. The recording may have stopped before any media arrived."
        )
    return info


def finalize_file(filename, *, live):
    path = Path(filename).resolve()
    info = probe_media(path)
    formats = info.get("format", {}).get("format_name", "").split(",")
    if not live or path.suffix.lower() != ".mp4" or "mpegts" not in formats:
        return path, info

    # A replaceable backend can write transport-stream data to an .mp4 path.
    # Inspect actual media instead of relying on its protocol name or suffix.
    # Keep the original until remuxing AND verification have both succeeded.
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}.finalizing-", suffix=".mp4", dir=path.parent, delete=False,
    ) as output:
        temporary = Path(output.name)
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(path), "-map", "0:v?", "-map", "0:a?", "-c", "copy",
             "-movflags", "+faststart", "-f", "mp4", str(temporary)],
            stdin=subprocess.DEVNULL,
        )
        if result.returncode:
            raise RuntimeError("Could not finalize the recording as MP4. The original recording has been kept.")
        verified = probe_media(temporary)
        if "mp4" not in verified.get("format", {}).get("format_name", "").split(","):
            raise RuntimeError("MP4 finalization did not produce an MP4 file. The original recording has been kept.")
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        with temporary.open("rb") as output:
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path, verified


def publish_file(path, filename):
    """Reserve a readable name before moving media; never replace an old file."""
    destination = path.with_name(filename)
    number = 1
    while True:
        try:
            with destination.open("xb"):
                pass
        except FileExistsError:
            number += 1
            name = Path(filename)
            destination = path.with_name(f"{name.stem} ({number}){name.suffix}")
            continue
        try:
            path.replace(destination)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return destination


def probe_url(url, events):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, UserNotLive

    options = {"quiet": True, "noplaylist": True, "skip_download": True,
               "socket_timeout": 15, "retries": 0, "extractor_retries": 0,
               "ignore_no_formats_error": True}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as error:
        cause = error.exc_info[1] if error.exc_info else None
        if isinstance(cause, UserNotLive):
            events.send("probe", live=False)
            return
        raise
    if not info or info.get("_type") in ("playlist", "multi_video"):
        raise ValueError("Use a single video or livestream page for live checks, not a playlist.")
    events.send("probe", live=bool(info.get("is_live") or info.get("live_status") == "is_live"))


def run_job(url, directory, job_id, events, control, *, live_only=False):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor.common import PostProcessor

    files = []
    live = False
    last_progress = None

    class FilePP(PostProcessor):
        def run(self, info):
            path, media = finalize_file(
                info["filepath"],
                live=bool(info.get("is_live") or info.get("live_status") == "is_live"),
            )
            filename = Path(self._downloader.prepare_filename(
                info, outtmpl="%(title).150B %(epoch>%Y-%m-%d %H_%M)s.%(ext)s",
            )).name
            path = publish_file(path, filename)
            info["filepath"] = str(path)
            try:
                duration = float(media.get("format", {}).get("duration"))
            except (TypeError, ValueError):
                duration = None
            if duration is not None and (not math.isfinite(duration) or duration < 0):
                duration = None
            files.append((path, duration))
            return [], info

    def metadata(info, *, incomplete):
        nonlocal live
        if incomplete:
            return
        live = bool(info.get("is_live") or info.get("live_status") == "is_live")
        events.send("metadata", title=info.get("title") or "Untitled", live=live)
        if live_only and not live:
            return "The stream is no longer live."

    def progress(data):
        nonlocal last_progress
        if live or data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if total:
            percent = 100 * (data.get("downloaded_bytes") or 0) / total
        elif data.get("fragment_count"):
            percent = 100 * (data.get("fragment_index") or 0) / data["fragment_count"]
        else:
            return
        value = f"{min(percent, 100):.1f}%"
        if value != last_progress:
            last_progress = value
            events.send("progress", progress=value)

    def postprocess(data):
        if not live and data.get("status") == "started":
            events.send("finalizing")

    options = {
        "paths": {"home": str(Path(directory).resolve())},
        # Isolate partial files until verified media gets its readable name.
        "outtmpl": {"default": f".%(title).150B [{job_id}].%(ext)s"},
        "match_filter": metadata,
        "progress_hooks": [progress],
        "postprocessor_hooks": [postprocess],
        "noplaylist": live_only,
    }
    with controlled_ffmpeg(control), YoutubeDL(options) as ydl:
        ydl.add_post_processor(FilePP(), when="after_move")
        result = ydl.download([url])
    if result:
        raise RuntimeError(f"yt-dlp could not complete the download (exit code {result}).")
    if not files:
        raise RuntimeError("The stream is no longer live." if live_only and not live else "The download finished without a media file.")
    control.finish()
    path, duration = files[-1]
    events.send("complete", file=str(path), duration=duration)


def main():
    # Preserve a private event pipe before redirecting process-level stdout.
    # FD redirection also captures output inherited by backend subprocesses.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1) as stream:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        events = Events(stream)
        control = RecordingControl(events)
        try:
            if len(sys.argv) == 3 and sys.argv[1] == "--probe":
                probe_url(sys.argv[2], events)
            else:
                live_only = len(sys.argv) == 5 and sys.argv[4] == "--live-only"
                if len(sys.argv) != 4 and not live_only:
                    raise ValueError("Expected URL, download directory, and job ID.")
                signal.signal(signal.SIGTERM, cancel_transfer)
                Thread(target=read_commands, args=(sys.stdin, control), daemon=True).start()
                run_job(*sys.argv[1:4], events, control, live_only=live_only)
        except DownloadStopped:
            control.finish()
            events.send("stopped")
        except Exception as error:
            control.finish()
            events.send("error", error=str(error))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
