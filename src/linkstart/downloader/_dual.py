"""Dual-loop recording strategy — two concurrent yt-dlp instances
(live-from-start + edge), deduped by timeline coverage. Used by YouTube."""
import asyncio
import time
from pathlib import Path

from linkstart.cooldown import Cooldown
from linkstart.downloader._loop import InterruptCallback
from linkstart.downloader._cleanup import cleanup_dual
from linkstart.downloader._stall import NoProgressStallPolicy
from linkstart.downloader._strategy import RecordingStrategy
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform


class DualRecordingStrategy(RecordingStrategy):
    # Larger grace/stall thresholds than edge: DASH manifest resolution can delay first bytes.
    DUAL_STALL_SEC: float = 300.0
    DUAL_STALL_GRACE_SEC: float = 180.0
    FULL_LOOP_SLEEP: float = 5.0
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
        interrupted_cooldown = Cooldown(self.INTERRUPTED_DEDUP_SEC)

        def _unique_output(stem: str) -> Path:
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

        stall_policy = NoProgressStallPolicy(
            stall_sec=self.DUAL_STALL_SEC,
            grace_sec=self.DUAL_STALL_GRACE_SEC,
            poll_sec=ctx.WATCHDOG_POLL_SEC,
        )

        def _loop(stem: str, *, live_from_start: bool, sleep: float):
            return ctx.attempt_loop(
                loop_name=stem,
                next_output=lambda attempt: _unique_output(stem),
                platform=platform, channel=channel, live=live,
                live_from_start=live_from_start,
                sleep=sleep,
                on_interrupted=on_interrupted,
                stop_event=stop_event,
                interrupted_cooldown=interrupted_cooldown,
                stall_policy=stall_policy,
                no_output_fail_limit=self.NO_OUTPUT_FAIL_LIMIT,
            )

        full_task = asyncio.create_task(
            _loop("full", live_from_start=True, sleep=self.FULL_LOOP_SLEEP)
        )
        edge_task = asyncio.create_task(
            _loop("edge", live_from_start=False, sleep=self.EDGE_LOOP_SLEEP)
        )
        try:
            (full_retries, full_error), (edge_retries, edge_error) = await asyncio.gather(
                full_task, edge_task
            )
        except BaseException:
            for t in (full_task, edge_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(full_task, edge_task, return_exceptions=True)
            raise

        result = await cleanup_dual(
            ctx.paths, ctx.media, channel, live, parts_dir,
            retry_count=full_retries + edge_retries,
            full_restarted=full_retries > 0,
        )
        loop_error = full_error or edge_error
        if not result.success and loop_error:
            result.error = (
                f"{result.error}: {loop_error}" if result.error else loop_error
            )
        return result
