"""RecordingStrategy — interface for how a single broadcast is recorded."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from linkstart.downloader._context import RecordingContext

from linkstart.downloader._loop import InterruptCallback
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo
from linkstart.platforms.base import Platform


class RecordingStrategy(ABC):
    INTERRUPTED_DEDUP_SEC: float = 300.0
    NO_OUTPUT_FAIL_LIMIT: int = 3

    def __init__(self, ctx: "RecordingContext") -> None:
        self.ctx = ctx

    @abstractmethod
    async def record(
        self,
        channel: ChannelConfig,
        platform: Platform,
        live: LiveInfo,
        on_interrupted: "InterruptCallback | None",
        stop_event=None,
    ) -> DownloadResult:
        """Record `live` to completion and return its result."""
