"""Protocol exposing the shared recording collaborators a RecordingStrategy needs."""
import asyncio
from pathlib import Path
from typing import Callable, Protocol

from linkstart.downloader._media import MediaTools
from linkstart.downloader._paths import RecordingPaths
from linkstart.downloader._process import ProcessRunner
from linkstart.downloader._stall import StallPolicy
from linkstart.cooldown import Cooldown
from linkstart.models import ChannelConfig, LiveInfo
from linkstart.platforms.base import Platform
from linkstart.downloader._loop import InterruptCallback
from linkstart.downloader._watchdog import RecordingLabel


class RecordingContext(Protocol):
    paths: RecordingPaths
    process: ProcessRunner
    media: MediaTools
    WATCHDOG_POLL_SEC: float

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
    ) -> tuple[int, str | None]: ...

    async def run_attempt(
        self,
        args: list[str],
        stop_event: asyncio.Event | None,
        *,
        output: Path | None = None,
        stall_policy: StallPolicy,
        label: "RecordingLabel | None" = None,
    ) -> tuple[int | None, bytes, bool]: ...
