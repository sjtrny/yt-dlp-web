"""A neutral localhost-only extractor used by container integration checks."""

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import UserNotLive


class ContainerSmokeIE(InfoExtractor):
    _VALID_URL = r"http://127\.0\.0\.1:(?P<port>[0-9]+)/fixture/(?P<id>[a-z0-9-]+)$"

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        video_id = match.group("id")
        origin = f"http://127.0.0.1:{match.group('port')}"
        if video_id == "task-offline":
            raise UserNotLive(video_id=video_id)
        if video_id.startswith("live-") or video_id == "vod-ffmpeg":
            if video_id == "live-delayed":
                self._download_webpage(f"{origin}/metadata/delayed", video_id, note=False)
            return {
                "id": video_id,
                "title": f"Container fixture {video_id}",
                "url": f"{origin}/live.m3u8",
                "ext": "mp4",
                "protocol": "m3u8",
                "is_live": (self._download_webpage(f"{origin}/metadata/flip", video_id, note=False).strip() == "live")
                if video_id == "live-flips" else video_id != "vod-ffmpeg",
            }
        return {
            "id": video_id,
            "title": "Container fixture collision" if video_id.startswith("collision-") else f"Container fixture {video_id}",
            "epoch": 1789518360,
            "url": f"{origin}/media/{video_id}.wav",
            "ext": "wav",
            "vcodec": "none",
            "acodec": "pcm_s16le",
        }
