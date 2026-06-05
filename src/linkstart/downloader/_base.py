"""Shared state and helpers used by every Downloader recording strategy."""
import asyncio
import contextlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from linkstart.cooldown import Cooldown
from linkstart.models import (
    ChannelConfig,
    Event,
    EventType,
    LiveInfo,
)
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[\/\\:*?"<>|\x00-\x1f]')
_MAX_TITLE_LEN = 80

# filename → epoch parser:  {full|edge}.{epoch}[_{NN}].mp4
_EPOCH_RE = re.compile(r"^(?:full|edge)\.(\d+)(?:_\d{2})?\.mp4$")


def sanitize_title(title: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", title).strip()
    cleaned = cleaned[:_MAX_TITLE_LEN]
    return cleaned or "untitled"


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        candidate = target.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("could not find a unique filename")


def _parse_epoch(filename: str) -> int | None:
    """Extract the unix timestamp from a 'full.{epoch}[_NN].mp4' filename."""
    m = _EPOCH_RE.match(filename)
    return int(m.group(1)) if m else None


InterruptCallback = Callable[[Event], Awaitable[None]]


class _DownloaderBase:
    # Finite retries + constant 3s sleep so yt-dlp actually exits within ~15s
    # when the broadcast ends. yt-dlp's default retry-sleep is exponential, so
    # `--retries 10` alone could mean ~17 min worst case. The outer loop's
    # _is_still_live_with_retries handles transient platform-API errors.
    YT_DLP_BASE_ARGS: tuple[str, ...] = (
        "--retries", "5",
        "--fragment-retries", "5",
        "--retry-sleep", "3",
    )
    FULL_LOOP_SLEEP: float = 5.0
    EDGE_LOOP_SLEEP: float = 0.0
    # An edge file is kept only if it adds at least this many seconds of
    # NEW timeline coverage beyond (base ∪ already-kept edges). Absorbs
    # ffprobe precision and HLS segment-alignment noise without discarding
    # genuinely-unique short tails.
    MIN_UNIQUE_COVERAGE_SEC: int = 5
    # After yt-dlp exits, retry the platform's is_still_live this many times
    # with this delay before concluding the broadcast actually ended. Prevents
    # premature finalization when yt-dlp dies on a network blip and the
    # platform API check happens to fail at the same instant.
    IS_STILL_LIVE_RETRIES: int = 3
    IS_STILL_LIVE_RETRY_DELAY: float = 5.0
    # Suppress repeat INTERRUPTED notifications within this window so flaky
    # uplinks don't spam Discord on long broadcasts.
    INTERRUPTED_DEDUP_SEC: float = 300.0
    NO_OUTPUT_FAIL_LIMIT: int = 3

    def __init__(self, yt_dlp_bin: str = "yt-dlp", ffmpeg_bin: str = "ffmpeg",
                 ffprobe_bin: str = "ffprobe") -> None:
        self.yt_dlp_bin = yt_dlp_bin
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin

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
        args += platform.yt_dlp_args(channel)
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

    def _make_parts_dir(self, channel: ChannelConfig, live: LiveInfo) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        safe_title = sanitize_title(live.title or live.live_id)
        out_dir = channel.save_dir / channel.platform / channel.display_name
        out_dir.mkdir(parents=True, exist_ok=True)
        parts_dir = out_dir / f"{date}_{safe_title}.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        return parts_dir

    def _final_path(self, channel: ChannelConfig, live: LiveInfo) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        safe_title = sanitize_title(live.title or live.live_id)
        out_dir = channel.save_dir / channel.platform / channel.display_name
        return out_dir / f"{date}_{safe_title}.mp4"

    async def _run_proc(
        self,
        args: list[str],
        *,
        capture_stdout: bool = False,
    ) -> tuple[int | None, bytes, bytes]:
        """Run a subprocess, always capturing stderr. Returns
        (returncode, stdout, stderr). On cancellation or unhandled error,
        terminate the child cleanly so it cannot survive as an orphan once
        this coroutine unwinds.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await proc.communicate()
            return proc.returncode, stdout or b"", stderr or b""
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await proc.wait()
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()

    def _attempt_produced_output(self, output: Path) -> bool:
        return any(
            p.stat().st_size > 0
            for p in output.parent.glob(output.stem + "*")
        )

    async def _attempt_loop(
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
    ) -> tuple[int, str | None]:
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
            returncode, stderr = await self._run_proc_with_stop(args, stop_event)
            if returncode != 0:
                log.warning(
                    "yt-dlp %s exited with %s: %s",
                    loop_name, returncode,
                    stderr.decode(errors="replace")[:200],
                )

            if self._attempt_produced_output(output):
                no_output_streak = 0
            else:
                no_output_streak += 1
                if no_output_streak >= self.NO_OUTPUT_FAIL_LIMIT:
                    fail_error = (
                        f"yt-dlp exited {no_output_streak}x without producing "
                        f"output: {stderr.decode(errors='replace').strip()[:300]}"
                    )
                    log.error(
                        "giving up on %s loop for %s/%s: %s",
                        loop_name, channel.platform, channel.log_name, fail_error,
                    )
                    return retries, fail_error

            if stop_event is not None and stop_event.is_set():
                return retries, None

            if not await self._is_still_live_with_retries(platform, channel, live):
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

    async def _run_proc_with_stop(
        self,
        args: list[str],
        stop_event: asyncio.Event | None,
    ) -> tuple[int | None, bytes]:
        """Run yt-dlp; if `stop_event` fires while it is running, send SIGTERM
        so yt-dlp can flush its current output before exiting. Returns
        (returncode, stderr). Falls back to SIGKILL after 10s.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        comm_task = asyncio.create_task(proc.communicate())
        stop_task: asyncio.Task | None = None
        if stop_event is not None:
            stop_task = asyncio.create_task(stop_event.wait())

        try:
            wait_tasks: list[asyncio.Task] = [comm_task]
            if stop_task is not None:
                wait_tasks.append(stop_task)
            done, _pending = await asyncio.wait(
                wait_tasks, return_when=asyncio.FIRST_COMPLETED
            )

            if stop_task is not None and stop_task in done and comm_task not in done:
                # Stop signaled — request graceful shutdown of yt-dlp.
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    _, stderr = await asyncio.wait_for(comm_task, timeout=10)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    _, stderr = await comm_task
            else:
                _, stderr = await comm_task
            return proc.returncode, stderr or b""
        finally:
            if stop_task is not None and not stop_task.done():
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            # Last-resort orphan cleanup (e.g. outer cancellation).
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await proc.wait()
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()

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
