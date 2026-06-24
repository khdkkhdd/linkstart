"""YouTube platform — live detection via yt-dlp `--print`.

Avoids requiring a YouTube Data API key by piggy-backing on yt-dlp's
extractor. `live_status` distinguishes is_live / is_upcoming / was_live.
"""
import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from linkstart.models import ChannelConfig, LiveInfo
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


class YoutubePlatform(Platform):
    name = "youtube"
    supports_live_from_start = True
    default_format = "137+140/bestvideo+bestaudio/best"

    CHECK_LIVE_TIMEOUT: float = 30.0
    TERMINATE_TIMEOUT: float = 5.0

    _PRINT_TEMPLATE = (
        "%(live_status)s\n%(id)s\n%(title)s\n%(thumbnail)s\n%(release_timestamp)s"
    )

    def __init__(self, yt_dlp_bin: str = "yt-dlp") -> None:
        self.yt_dlp_bin = yt_dlp_bin

    def recording_strategy(self, ctx):
        from linkstart.downloader._dual import DualRecordingStrategy
        return DualRecordingStrategy(ctx)

    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        return f"https://www.youtube.com/{channel.channel_id}/live"

    async def check_live(self, channel: ChannelConfig) -> LiveInfo | None:
        url = self.build_url(channel, None)  # type: ignore[arg-type]
        args: list[str] = [
            self.yt_dlp_bin,
            "--print",
            self._PRINT_TEMPLATE,
            "--no-warnings",
            "--skip-download",
        ]
        if channel.cookies_from_browser:
            args += ["--cookies-from-browser", channel.cookies_from_browser]
        args.append(url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            log.warning("youtube: yt-dlp invocation failed for %s: %s", channel.channel_id, e)
            return None

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.CHECK_LIVE_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning(
                "youtube: yt-dlp timed out after %.0fs for %s",
                self.CHECK_LIVE_TIMEOUT, channel.channel_id,
            )
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.TERMINATE_TIMEOUT)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            return None
        except OSError as e:
            log.warning("youtube: yt-dlp invocation failed for %s: %s", channel.channel_id, e)
            return None

        if proc.returncode != 0:
            return None

        lines = stdout.decode(errors="replace").strip().splitlines()
        if len(lines) < 3:
            return None

        status, video_id, title = lines[0], lines[1], lines[2]
        thumbnail_raw = lines[3] if len(lines) >= 4 else ""
        thumbnail = thumbnail_raw if thumbnail_raw and thumbnail_raw != "NA" else None
        release_raw = lines[4] if len(lines) >= 5 else ""
        started_at = None
        if release_raw and release_raw != "NA":
            try:
                started_at = datetime.fromtimestamp(
                    int(float(release_raw)), tz=timezone.utc
                )
            except ValueError:
                started_at = None

        if status != "is_live":
            return None
        if not video_id or video_id == "NA":
            return None

        return LiveInfo(
            live_id=video_id,
            title=title if title != "NA" else "",
            url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=thumbnail,
            started_at=started_at,
        )
