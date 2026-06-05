"""yt-dlp wrapper with retry, dual-loop, and ffmpeg concat/dedup.

This package splits the recording strategies into focused modules:
- _base: shared state, _run_proc, _notify_interrupted, helpers
- _edge: single-loop "edge only" recorder (TwitCasting, Chzzk)
- _dual: two-loop "live-from-start + edge" recorder (YouTube)
- _cleanup: dual-mode dedup, ffprobe/ffmpeg subprocess wrappers
"""
import asyncio

from linkstart.downloader._base import (
    InterruptCallback,
    _DownloaderBase,
    _parse_epoch,
    sanitize_title,
    unique_path,
)
from linkstart.downloader._cleanup import _DualCleanupMixin
from linkstart.downloader._dual import _DualRecorderMixin
from linkstart.downloader._edge import _EdgeRecorderMixin
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform


class Downloader(
    _DualCleanupMixin,
    _DualRecorderMixin,
    _EdgeRecorderMixin,
    _DownloaderBase,
):
    async def record(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        on_interrupted: InterruptCallback | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> DownloadResult:
        if platform.supports_live_from_start:
            result = await self._record_dual(
                channel, platform, live, on_interrupted, stop_event
            )
        else:
            result = await self._record_edge_only(
                channel, platform, live, on_interrupted, stop_event
            )
        if result.success and result.file_path is not None:
            result.validation = await platform.validate_recording(result.file_path)
        return result


__all__ = [
    "Downloader",
    "InterruptCallback",
    "_parse_epoch",
    "sanitize_title",
    "unique_path",
]
