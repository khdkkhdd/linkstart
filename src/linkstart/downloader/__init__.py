"""yt-dlp/ffmpeg recording engine — retry loop, stall watchdog, and platform strategies."""
import asyncio

from linkstart.downloader._loop import (
    InterruptCallback,
    _DownloaderBase,
)
from linkstart.downloader._cleanup import _parse_epoch
from linkstart.downloader._paths import RecordingPaths, sanitize_title, unique_path
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform


class Downloader(_DownloaderBase):
    """Owns shared recording infrastructure; delegates the actual recording
    flow to the platform's declared RecordingStrategy."""

    async def record(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        on_interrupted: InterruptCallback | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> DownloadResult:
        strategy = platform.recording_strategy(self)
        result = await strategy.record(
            channel, platform, live, on_interrupted, stop_event
        )
        if result.success and result.file_path is not None:
            result.validation = await platform.validate_recording(result.file_path)
        return result


__all__ = [
    "Downloader",
    "InterruptCallback",
    "RecordingPaths",
    "_parse_epoch",
    "sanitize_title",
    "unique_path",
]
