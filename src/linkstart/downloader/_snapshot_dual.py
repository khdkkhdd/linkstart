"""Snapshot-dual recording strategy — an edge live loop bracketed by two VOD
snapshots (at detection and after broadcast end), merged by cleanup_dual.
Used by platforms (CI.ME/IVS) whose from-start source is a growing VOD
playlist that terminates every fetch (ENDLIST), where a full *loop* would
re-download the whole broadcast on each restart."""
import asyncio
import logging
import time
from pathlib import Path

from linkstart.cooldown import Cooldown
from linkstart.downloader._cleanup import cleanup_dual
from linkstart.downloader._loop import InterruptCallback
from linkstart.downloader._process import _stderr_excerpt
from linkstart.downloader._stall import (
    AnyStallPolicy,
    MinThroughputStallPolicy,
    NoProgressStallPolicy,
)
from linkstart.downloader._strategy import RecordingStrategy
from linkstart.downloader._watchdog import RecordingLabel
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


def _unique_epoch_output(parts_dir: Path, stem: str) -> Path:
    """Return parts_dir/{stem}.{epoch}.mp4, suffixing _NN on collision."""
    epoch = int(time.time())
    base = parts_dir / f"{stem}.{epoch}.mp4"
    if not base.exists():
        return base
    for i in range(1, 100):
        cand = parts_dir / f"{stem}.{epoch}_{i:02d}.mp4"
        if not cand.exists():
            return cand
    raise RuntimeError("could not find unique output filename")


class SnapshotDualRecordingStrategy(RecordingStrategy):
    EDGE_STALL_SEC: float = 45.0
    EDGE_STALL_GRACE_SEC: float = 30.0
    EDGE_MIN_BYTES_PER_SEC: float = 1024.0
    EDGE_MIN_RATE_WINDOW_SEC: float = 60.0
    EDGE_LOOP_SLEEP: float = 0.0
    SNAPSHOT_STALL_SEC: float = 300.0
    SNAPSHOT_STALL_GRACE_SEC: float = 180.0
    # Final-snapshot retry absorbs IVS's lag writing the last segments/playlist.
    FINAL_SNAPSHOT_ATTEMPTS: int = 2
    FINAL_SNAPSHOT_RETRY_DELAY: float = 30.0

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

        edge_task = asyncio.create_task(
            ctx.attempt_loop(
                loop_name="edge",
                next_output=lambda attempt: _unique_epoch_output(parts_dir, "edge"),
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
        )
        initial_task = asyncio.create_task(
            self._snapshot(channel, platform, live, parts_dir, stop_event)
        )
        try:
            (edge_retries, edge_error), _ = await asyncio.gather(
                edge_task, initial_task
            )
        except BaseException:
            for t in (edge_task, initial_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(edge_task, initial_task, return_exceptions=True)
            raise

        want_final = (
            (stop_event is None or not stop_event.is_set())
            and platform.build_full_url(channel, live) is not None
        )
        if want_final:
            for attempt in range(self.FINAL_SNAPSHOT_ATTEMPTS):
                if await self._snapshot(
                    channel, platform, live, parts_dir, stop_event
                ):
                    break
                if attempt < self.FINAL_SNAPSHOT_ATTEMPTS - 1:
                    await asyncio.sleep(self.FINAL_SNAPSHOT_RETRY_DELAY)

        result = await cleanup_dual(
            ctx.paths, ctx.media, channel, live, parts_dir,
            retry_count=edge_retries,
            # Snapshot epochs are download times, not broadcast start.
            full_restarted=True,
        )
        if not result.success and edge_error:
            result.error = (
                f"{result.error}: {edge_error}" if result.error else edge_error
            )
        return result

    async def _snapshot(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        parts_dir: Path,
        stop_event: asyncio.Event | None,
    ) -> bool:
        """Download one finite VOD snapshot as full.{epoch}.mp4; True on output."""
        ctx = self.ctx
        full_url = platform.build_full_url(channel, live)
        if not full_url:
            log.debug(
                "no full (VOD) URL for %s/%s — skipping snapshot",
                channel.platform, channel.log_name,
            )
            return False

        output = _unique_epoch_output(parts_dir, "full")
        args: list[str] = [ctx.yt_dlp_bin, *ctx.YT_DLP_BASE_ARGS]
        # Snapshots are finite VOD fetches — no --hls-use-mpegts.
        args += [
            a for a in platform.download_profile(channel).to_yt_dlp_args()
            if a != "--hls-use-mpegts"
        ]
        fmt = channel.format if channel.format is not None else platform.default_format
        if fmt:
            args += ["-f", fmt]
        if channel.cookies_from_browser:
            args += ["--cookies-from-browser", channel.cookies_from_browser]
        args += ["-o", str(output), full_url]

        log.info(
            "yt-dlp full snapshot for %s/%s → %s",
            channel.platform, channel.log_name, output.name,
        )
        returncode, stderr, stalled = await ctx.run_attempt(
            args, stop_event, output=output,
            stall_policy=NoProgressStallPolicy(
                stall_sec=self.SNAPSHOT_STALL_SEC,
                grace_sec=self.SNAPSHOT_STALL_GRACE_SEC,
                poll_sec=ctx.WATCHDOG_POLL_SEC,
            ),
            label=RecordingLabel(channel.platform, channel.log_name, "full"),
        )
        produced = ctx._attempt_produced_output(output) and not stalled
        if not produced:
            log.warning(
                "full snapshot failed for %s/%s (rc=%s, stalled=%s): %s",
                channel.platform, channel.log_name, returncode, stalled,
                _stderr_excerpt(stderr),
            )
        return produced
