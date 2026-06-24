"""Abstract base class shared by all platform implementations."""
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path

from linkstart.models import (
    ChannelConfig,
    DownloadProfile,
    LiveInfo,
    ValidationResult,
)


class Platform(ABC):
    """Per-platform live detection and yt-dlp argument shaping."""

    name: str = ""
    supports_live_from_start: bool = False
    default_format: str | None = None

    @abstractmethod
    async def check_live(self, channel: ChannelConfig) -> LiveInfo | None:
        """Return LiveInfo if the channel is currently live, else None.

        Swallow transient errors (network, timeouts) and return None; only programming errors may raise.
        """

    @abstractmethod
    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        """URL to pass to yt-dlp for downloading."""

    def recording_strategy(self, ctx):
        """Return the RecordingStrategy for this platform; default is edge-only (HLS).

        Override to return a dual strategy for platforms that support recording from broadcast start.
        """
        from linkstart.downloader._edge import EdgeRecordingStrategy
        return EdgeRecordingStrategy(ctx)

    def download_profile(self, channel: ChannelConfig) -> DownloadProfile:
        """Return the yt-dlp profile (container, downloader, extra flags) for this channel.

        Merges the platform's base profile with any channel-level ``downloader`` override.
        """
        profile = self._base_download_profile(channel)
        if channel.downloader:
            profile = replace(profile, downloader=channel.downloader)
        return profile

    def _base_download_profile(self, channel: ChannelConfig) -> DownloadProfile:
        """Platform's intrinsic profile; subclasses override (e.g. HLS platforms use mpegts)."""
        return DownloadProfile()

    async def is_still_live(self, channel: ChannelConfig, live_id: str) -> bool:
        """True if the same broadcast (live_id) is still active; subclasses may override with a cheaper check."""
        current = await self.check_live(channel)
        return current is not None and current.live_id == live_id

    def get_auth_cookies(self, channel: ChannelConfig) -> dict[str, str] | None:
        """Return cookies to attach to live-check HTTP requests."""
        return None

    async def close(self) -> None:
        """Release platform-owned resources (HTTP sessions, etc.)."""
        return None

    async def validate_recording(self, file_path: Path) -> ValidationResult:
        """Check a finished recording for silent-failure signatures; default accepts all."""
        return ValidationResult(status="ok")
