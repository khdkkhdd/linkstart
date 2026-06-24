"""Shared state and helpers used by every Downloader recording strategy."""
import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Awaitable, Callable

from linkstart.cooldown import Cooldown
from linkstart.downloader._media import MediaTools
from linkstart.downloader._paths import RecordingPaths
from linkstart.downloader._process import ProcessRunner
from linkstart.downloader._stall import NeverAbortStallPolicy, StallPolicy
from linkstart.downloader._watchdog import (
    HEARTBEAT_INTERVAL_SEC as _HB_INTERVAL_SEC,
    HEARTBEAT_PROBE_TIMEOUT_SEC as _HB_PROBE_TIMEOUT_SEC,
    WATCHDOG_POLL_SEC as _WD_POLL_SEC,
    Heartbeat,
    RecordingLabel,
    StallWatchdog,
    scan_output,
)
from linkstart.models import (
    ChannelConfig,
    Event,
    EventType,
    LiveInfo,
)
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


def _stderr_excerpt(stderr: bytes, limit: int = 800) -> str:
    """Return the tail of subprocess stderr (failures appear at the end, not the head)."""
    text = stderr.decode(errors="replace").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


InterruptCallback = Callable[[Event], Awaitable[None]]


class _DownloaderBase:
    # Constant retry-sleep avoids yt-dlp's default exponential back-off (~17 min worst-case).
    YT_DLP_BASE_ARGS: tuple[str, ...] = (
        "--retries", "5",
        "--fragment-retries", "5",
        "--retry-sleep", "3",
    )
    # Retry is_still_live after yt-dlp exits to avoid premature finalization on transient API failures.
    IS_STILL_LIVE_RETRIES: int = 3
    IS_STILL_LIVE_RETRY_DELAY: float = 5.0
    WATCHDOG_POLL_SEC: float = _WD_POLL_SEC
    HEARTBEAT_INTERVAL_SEC: float = _HB_INTERVAL_SEC
    HEARTBEAT_PROBE_TIMEOUT_SEC: float = _HB_PROBE_TIMEOUT_SEC
    def __init__(self, yt_dlp_bin: str = "yt-dlp", ffmpeg_bin: str = "ffmpeg",
                 ffprobe_bin: str = "ffprobe") -> None:
        self.yt_dlp_bin = yt_dlp_bin
        self.paths = RecordingPaths()
        self.process = ProcessRunner()
        self.media = MediaTools(self.process, ffmpeg_bin, ffprobe_bin)

    def _build_args(
        self,
        platform: Platform,
        channel: ChannelConfig,
        live: LiveInfo,
        *,
        live_from_start: bool,
        output: Path,
    ) -> list[str]:
        args: list[str] = [self.yt_dlp_bin, *self.YT_DLP_BASE_ARGS]
        if live_from_start:
            args.append("--live-from-start")
        args += platform.download_profile(channel).to_yt_dlp_args()
        # channel.format takes precedence; explicit empty string suppresses -f entirely
        if channel.format is None:
            fmt = platform.default_format
        else:
            fmt = channel.format
        if fmt:
            args += ["-f", fmt]
        if channel.cookies_from_browser:
            args += ["--cookies-from-browser", channel.cookies_from_browser]
        args += ["-o", str(output), platform.build_url(channel, live)]
        return args

    def _attempt_produced_output(self, output: Path) -> bool:
        return scan_output(output)[0] > 0

    async def attempt_loop(
        self,
        *,
        loop_name: str,
        next_output: Callable[[int], Path],
        platform: Platform,
        channel: ChannelConfig,
        live: LiveInfo,
        live_from_start: bool,
        sleep: float,
        on_interrupted: "InterruptCallback | None",
        stop_event: asyncio.Event | None,
        interrupted_cooldown: Cooldown,
        stall_policy: StallPolicy,
        no_output_fail_limit: int,
    ) -> tuple[int, str | None]:
        """Run yt-dlp, restarting while the broadcast stays live; return (restarts, error)."""
        attempt = 0
        retries = 0
        no_output_streak = 0
        while True:
            output = next_output(attempt)
            args = self._build_args(
                platform, channel, live,
                live_from_start=live_from_start, output=output,
            )
            log.info(
                "yt-dlp %s attempt %s for %s/%s → %s",
                loop_name, attempt, channel.platform, channel.log_name,
                output.name,
            )
            returncode, stderr, stalled = await self.run_attempt(
                args, stop_event, output=output, stall_policy=stall_policy,
                label=RecordingLabel(channel.platform, channel.log_name, loop_name),
            )
            if stalled:
                log.warning(
                    "yt-dlp %s stalled (no output growth) for %s/%s — aborted",
                    loop_name, channel.platform, channel.log_name,
                )
            elif returncode != 0:
                log.warning(
                    "yt-dlp %s exited with %s: %s",
                    loop_name, returncode,
                    _stderr_excerpt(stderr),
                )

            # Stalled attempts count as no-output even if a stub was written.
            if self._attempt_produced_output(output) and not stalled:
                no_output_streak = 0
            else:
                no_output_streak += 1
                if no_output_streak >= no_output_fail_limit:
                    if stalled:
                        fail_error = (
                            f"yt-dlp stalled (no output growth) "
                            f"{no_output_streak}x — stream could not be pulled"
                        )
                    else:
                        fail_error = (
                            f"yt-dlp exited {no_output_streak}x without producing "
                            f"output: {_stderr_excerpt(stderr)}"
                        )
                    log.error(
                        "giving up on %s loop for %s/%s: %s",
                        loop_name, channel.platform, channel.log_name, fail_error,
                    )
                    return retries, fail_error

            if stop_event is not None and stop_event.is_set():
                return retries, None

            if not await self._is_still_live_with_retries(platform, channel, live):
                log.info(
                    "broadcast ended: %s/%s id=%s (%s loop, %s restart(s))",
                    channel.platform, channel.log_name, live.live_id,
                    loop_name, retries,
                )
                return retries, None

            retries += 1
            await self._notify_interrupted(
                on_interrupted, channel, live,
                loop=loop_name, attempt=retries, cooldown=interrupted_cooldown,
            )
            attempt += 1
            if sleep > 0:
                await asyncio.sleep(sleep)

    async def _is_still_live_with_retries(
        self,
        platform: Platform,
        channel: ChannelConfig,
        live: LiveInfo,
    ) -> bool:
        """Confirm broadcast is still live, tolerating transient API failures.

        Calls platform.is_still_live up to IS_STILL_LIVE_RETRIES times with
        IS_STILL_LIVE_RETRY_DELAY seconds between attempts. Returns True on
        the first positive answer; False only after every attempt says no.
        """
        for attempt in range(self.IS_STILL_LIVE_RETRIES):
            if await platform.is_still_live(channel, live.live_id):
                return True
            if attempt < self.IS_STILL_LIVE_RETRIES - 1:
                await asyncio.sleep(self.IS_STILL_LIVE_RETRY_DELAY)
        return False

    async def run_attempt(
        self,
        args: list[str],
        stop_event: asyncio.Event | None,
        *,
        output: Path | None = None,
        stall_policy: StallPolicy,
        label: RecordingLabel | None = None,
    ) -> tuple[int | None, bytes, bool]:
        """Run yt-dlp until it exits, stop_event fires, or the stall watchdog trips; return (returncode, stderr, stalled)."""
        proc = await self.process.spawn(args)
        comm_task = asyncio.create_task(proc.communicate())
        stop_task: asyncio.Task | None = None
        if stop_event is not None:
            stop_task = asyncio.create_task(stop_event.wait())
        watchdog_task: asyncio.Task | None = None
        if output is not None and not isinstance(stall_policy, NeverAbortStallPolicy):
            hb = Heartbeat(
                label, self.media,
                interval_sec=self.HEARTBEAT_INTERVAL_SEC,
                probe_timeout_sec=self.HEARTBEAT_PROBE_TIMEOUT_SEC,
            ) if label is not None else None
            watchdog_task = asyncio.create_task(
                StallWatchdog(output, stall_policy, hb).watch()
            )

        aux_tasks = [t for t in (stop_task, watchdog_task) if t is not None]
        try:
            done, _pending = await asyncio.wait(
                [comm_task, *aux_tasks], return_when=asyncio.FIRST_COMPLETED
            )

            stop_fired = stop_task is not None and stop_task in done
            stall_fired = watchdog_task is not None and watchdog_task in done
            stalled = stall_fired and comm_task not in done
            if comm_task not in done and (stop_fired or stall_fired):
                stderr = await self.process.drain_or_kill(proc, comm_task)
            else:
                _, stderr = await comm_task
            return proc.returncode, stderr or b"", stalled
        finally:
            for t in (*aux_tasks, comm_task):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            # Last-resort orphan cleanup (e.g. outer cancellation).
            await self.process.terminate_and_reap(proc)

    async def _notify_interrupted(
        self,
        on_interrupted: InterruptCallback | None,
        channel: ChannelConfig,
        live: LiveInfo,
        *,
        loop: str,
        attempt: int,
        cooldown: Cooldown,
    ) -> None:
        if on_interrupted is None:
            return
        if not cooldown.try_acquire():
            log.debug(
                "interrupted notify suppressed (dedup): %s/%s loop=%s attempt=%s",
                channel.platform, channel.channel_id, loop, attempt,
            )
            return
        try:
            await on_interrupted(
                Event(
                    type=EventType.DOWNLOAD_INTERRUPTED,
                    channel=channel,
                    live=live,
                    message=f"loop: {loop}   attempt: {attempt}",
                    retry_count=attempt,
                )
            )
        except Exception:
            log.exception("interrupted notifier failed")
