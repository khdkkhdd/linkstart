"""Edge-only recording loop — one yt-dlp instance, ffmpeg-concat parts at the end."""
import asyncio
import logging
import shutil
from pathlib import Path

from linkstart.cooldown import Cooldown
from linkstart.downloader._base import InterruptCallback, unique_path
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


class _EdgeRecorderMixin:
    async def _record_edge_only(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        on_interrupted: InterruptCallback | None,
        stop_event: asyncio.Event | None = None,
    ) -> DownloadResult:
        parts_dir = self._make_parts_dir(channel, live)
        event_loop = asyncio.get_running_loop()
        start_time = event_loop.time()

        retries, fail_error = await self._attempt_loop(
            loop_name="edge",
            next_output=lambda attempt: parts_dir / f"part{attempt:02d}.ts",
            platform=platform, channel=channel, live=live,
            live_from_start=False,
            sleep=self.EDGE_LOOP_SLEEP,
            on_interrupted=on_interrupted,
            stop_event=stop_event,
            interrupted_cooldown=Cooldown(self.INTERRUPTED_DEDUP_SEC),
        )

        part_files = sorted(parts_dir.glob("part*.ts"))
        if not part_files:
            return DownloadResult(
                success=False,
                error=fail_error or "no parts captured",
                retry_count=retries,
            )

        # Each attempt becomes its own .mp4. Concatenating would hide the gap
        # between attempts; better to keep the interruption boundaries visible.
        final_path = unique_path(self._final_path(channel, live))
        if not await self._remux_ts_to_mp4(part_files[0], final_path):
            return DownloadResult(
                success=False, error="ffmpeg remux failed", retry_count=retries
            )

        extras: list[Path] = []
        for i, part in enumerate(part_files[1:], start=1):
            extra_path = final_path.with_name(
                f"{final_path.stem}.part_{i:03d}.mp4"
            )
            if await self._remux_ts_to_mp4(part, extra_path):
                extras.append(extra_path)

        shutil.rmtree(parts_dir, ignore_errors=True)
        duration = int(event_loop.time() - start_time)
        size = final_path.stat().st_size if final_path.exists() else 0
        size += sum(p.stat().st_size for p in extras if p.exists())

        # Best-effort: append a recording record for the daily summary.
        # Never blocks success.
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
