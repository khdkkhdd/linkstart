"""Abstract base class shared by all platform implementations."""
from abc import ABC, abstractmethod
from pathlib import Path

from linkstart.models import ChannelConfig, LiveInfo, ValidationResult


class Platform(ABC):
    """Per-platform live detection and yt-dlp argument shaping."""

    name: str = ""
    supports_live_from_start: bool = False
    default_format: str | None = None

    @abstractmethod
    async def check_live(self, channel: ChannelConfig) -> LiveInfo | None:
        """Return LiveInfo if currently live, otherwise None.

        Transient errors (network, timeouts, malformed responses) should
        return None — never raise. Programming errors may still raise.

        Implementations that need auth for their live-state API should
        call self.get_auth_cookies(channel) and attach the result to the
        aiohttp request.
        """

    @abstractmethod
    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        """URL to pass to yt-dlp for downloading."""

    def yt_dlp_args(self, channel: ChannelConfig) -> list[str]:
        """Platform-specific args applied to BOTH full and edge loops.

        Does NOT include --live-from-start: the Downloader controls that
        based on which loop is running.

        Examples:
            TwitCasting → ["--hls-use-mpegts"]
            YouTube     → []
        """
        return []

    async def is_still_live(self, channel: ChannelConfig, live_id: str) -> bool:
        """True iff the same broadcast (live_id) is still active.

        Default implementation re-calls check_live. Subclasses may override
        with a cheaper endpoint (e.g. yt-dlp --print live_status).
        """
        current = await self.check_live(channel)
        return current is not None and current.live_id == live_id

    def get_auth_cookies(self, channel: ChannelConfig) -> dict[str, str] | None:
        """Return cookies to attach to live-check HTTP requests."""
        return None

    async def close(self) -> None:
        """Release platform-owned resources (HTTP sessions, etc.)."""
        return None

    async def validate_recording(self, file_path: Path) -> ValidationResult:
        """Sanity-check a produced recording for silent-failure signatures.

        Default is a no-op (`status="ok"`). Platforms with known silent-failure
        modes (e.g. TwitCasting's login-wall placeholder, which yt-dlp records
        as a real-looking file) should override to flag suspicious output.
        """
        return ValidationResult(status="ok")
