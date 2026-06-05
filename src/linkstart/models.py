"""Core data models shared across the package."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class LiveInfo:
    """Snapshot of a single live broadcast detected by a Platform."""

    live_id: str
    title: str
    url: str
    started_at: datetime | None = None
    thumbnail_url: str | None = None


@dataclass
class ChannelConfig:
    """Per-channel runtime configuration after defaults are merged in."""

    platform: str
    channel_id: str
    notifier_id: str | None = None
    poll_interval: int = 60
    cookies_from_browser: str | None = None
    save_dir: Path = field(default_factory=lambda: Path("recordings"))
    format: str | None = None
    # Human-readable display name for paths/logs/Discord. Falls back to
    # channel_id when not set (useful for opaque IDs like Chzzk UUIDs).
    alias: str | None = None

    @property
    def display_name(self) -> str:
        """Human-friendly name used for paths and Discord embeds."""
        return self.alias or self.channel_id

    @property
    def log_name(self) -> str:
        """Console-log identifier: shows alias and channel_id together when
        an alias is set, so operators can map display name to raw ID."""
        if self.alias:
            return f"{self.alias} (id={self.channel_id})"
        return self.channel_id


class EventType(str, Enum):
    LIVE_STARTED = "live_started"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_INTERRUPTED = "download_interrupted"
    DOWNLOAD_FINISHED = "download_finished"
    ERROR = "error"
    SUMMARY = "summary"


@dataclass
class Event:
    """A lifecycle event handed to Notifiers."""

    type: EventType
    channel: ChannelConfig
    live: LiveInfo | None = None
    message: str | None = None
    file_path: Path | None = None
    extra_files: list[Path] = field(default_factory=list)
    duration_sec: int | None = None
    size_bytes: int | None = None
    retry_count: int = 0


ValidationStatus = Literal["ok", "suspicious", "invalid"]


@dataclass(frozen=True)
class ValidationResult:
    """Post-download sanity check on a produced recording.

    A platform may override Platform.validate_recording to flag silent failures
    (e.g. TwitCasting's login-wall placeholder, which yt-dlp happily records as
    a real-looking but low-bitrate file). The default implementation returns
    status="ok" for every platform that doesn't override.
    """

    status: ValidationStatus
    reason: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class DownloadResult:
    """Return value of Downloader.record()."""

    success: bool
    file_path: Path | None = None
    extra_files: list[Path] = field(default_factory=list)
    error: str | None = None
    size_bytes: int = 0
    duration_sec: int = 0
    retry_count: int = 0
    validation: ValidationResult = field(
        default_factory=lambda: ValidationResult(status="ok")
    )
