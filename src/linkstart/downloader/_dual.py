"""Dual-loop recording — two concurrent yt-dlp instances (live-from-start + edge)."""
import asyncio
import time
from pathlib import Path

from linkstart.cooldown import Cooldown
from linkstart.downloader._base import InterruptCallback
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform


class _DualRecorderMixin:
    async def _record_dual(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        on_interrupted: InterruptCallback | None,
        stop_event: asyncio.Event | None = None,
    ) -> DownloadResult:
        parts_dir = self._make_parts_dir(channel, live)
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

        def _loop(stem: str, *, live_from_start: bool, sleep: float):
            return self._attempt_loop(
                loop_name=stem,
                next_output=lambda attempt: _unique_output(stem),
                platform=platform, channel=channel, live=live,
                live_from_start=live_from_start,
                sleep=sleep,
                on_interrupted=on_interrupted,
                stop_event=stop_event,
                interrupted_cooldown=interrupted_cooldown,
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
            # asyncio.gather propagates the first exception without cancelling
            # siblings. Cancel them explicitly so each loop's _run_proc
            # finally-block can terminate its child yt-dlp process.
            for t in (full_task, edge_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(full_task, edge_task, return_exceptions=True)
            raise

        result = await self._cleanup_dual(
            channel, live, parts_dir,
            retry_count=full_retries + edge_retries,
            full_restarted=full_retries > 0,
        )
        loop_error = full_error or edge_error
        if not result.success and loop_error:
            result.error = (
                f"{result.error}: {loop_error}" if result.error else loop_error
            )
        return result
