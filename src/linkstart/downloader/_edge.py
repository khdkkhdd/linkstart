"""Edge-only recording strategy — one yt-dlp instance, each attempt remuxed
into its own .mp4. Used by HLS-live platforms (TwitCasting, Chzzk)."""
import asyncio
import logging
import shutil
from pathlib import Path

from linkstart.cooldown import Cooldown
from linkstart.downloader._loop import InterruptCallback
from linkstart.downloader._paths import unique_path
from linkstart.downloader._stall import (
    AnyStallPolicy,
    MinThroughputStallPolicy,
    NoProgressStallPolicy,
)
from linkstart.downloader._strategy import RecordingStrategy
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


class EdgeRecordingStrategy(RecordingStrategy):
    EDGE_STALL_SEC: float = 45.0
    EDGE_STALL_GRACE_SEC: float = 30.0
    EDGE_MIN_BYTES_PER_SEC: float = 1024.0
    EDGE_MIN_RATE_WINDOW_SEC: float = 60.0
    EDGE_LOOP_SLEEP: float = 0.0

    async def record(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        on_interrupted: InterruptCallback | None,
        stop_event: asyncio.Event | None = None,
    ) -> DownloadResult:
        ctx = self.ctx
        parts_dir = ctx.paths.make_parts_dir(channel, live)
        event_loop = asyncio.get_running_loop()
        start_time = event_loop.time()

        suffix = platform.download_profile(channel).part_suffix

        retries, fail_error = await ctx.attempt_loop(
            loop_name="edge",
            next_output=lambda attempt: parts_dir / f"part{attempt:02d}{suffix}",
            platform=platform, channel=channel, live=live,
            live_from_start=False,
            sleep=self.EDGE_LOOP_SLEEP,
            on_interrupted=on_interrupted,
            stop_event=stop_event,
            interrupted_cooldown=Cooldown(self.INTERRUPTED_DEDUP_SEC),
            stall_policy=AnyStallPolicy([
                NoProgressStallPolicy(
                    stall_sec=self.EDGE_STALL_SEC,
                    grace_sec=self.EDGE_STALL_GRACE_SEC,
                    poll_sec=ctx.WATCHDOG_POLL_SEC,
                ),
                MinThroughputStallPolicy(
                    min_bytes_per_sec=self.EDGE_MIN_BYTES_PER_SEC,
                    window_sec=self.EDGE_MIN_RATE_WINDOW_SEC,
                    poll_sec=ctx.WATCHDOG_POLL_SEC,
                ),
            ]),
            no_output_fail_limit=self.NO_OUTPUT_FAIL_LIMIT,
        )

        part_files = sorted(parts_dir.glob(f"part*{suffix}"))
        if not part_files:
            ctx.paths.discard_parts_dir(parts_dir)
            return DownloadResult(
                success=False,
                error=fail_error or "no parts captured",
                retry_count=retries,
            )

        final_path = unique_path(ctx.paths.final_path(channel, live))
        if not await ctx.media.remux(part_files[0], final_path):
            ctx.paths.discard_parts_dir(parts_dir)
            return DownloadResult(
                success=False, error="ffmpeg remux failed", retry_count=retries
            )

        extras: list[Path] = []
        for i, part in enumerate(part_files[1:], start=1):
            extra_path = final_path.with_name(
                f"{final_path.stem}.part_{i:03d}.mp4"
            )
            if await ctx.media.remux(part, extra_path):
                extras.append(extra_path)

        shutil.rmtree(parts_dir, ignore_errors=True)
        duration = int(event_loop.time() - start_time)
        size = final_path.stat().st_size if final_path.exists() else 0
        size += sum(p.stat().st_size for p in extras if p.exists())

        try:
            from linkstart.summary import append_recording_record
            append_recording_record(
                platform=channel.platform,
                channel_id=channel.channel_id,
                file_path=final_path,
                size_bytes=size,
                duration_sec=duration,
            )
        except Exception:
            log.exception("failed to append recording record")

        return DownloadResult(
            success=True,
            file_path=final_path,
            extra_files=extras,
            size_bytes=size,
            duration_sec=duration,
            retry_count=retries,
        )
