"""YAML configuration loader and channel-defaults merger."""
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from linkstart.models import ChannelConfig


def _expand_path(v):
    """Pydantic-friendly: expand ~ and accept str | Path | None."""
    if v is None:
        return None
    return Path(v).expanduser()


class NotifierSpec(BaseModel):
    id: str
    type: str
    webhook_url: str


class SummarySpec(BaseModel):
    enabled: bool = False
    cron: str = "0 9 * * *"
    notifier: str | None = None


class RawChannel(BaseModel):
    platform: str
    channel_id: str
    notifier: str | None = None
    poll_interval: int | None = None
    cookies_from_browser: str | None = None
    save_dir: Path | None = None
    format: str | None = None
    alias: str | None = None
    downloader: str | None = None

    _expand_save_dir = field_validator("save_dir", mode="before")(_expand_path)


class Defaults(BaseModel):
    save_dir: Path = Path("recordings")
    poll_interval: int = 60

    _expand_save_dir = field_validator("save_dir", mode="before")(_expand_path)


class AppConfig(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    notifiers: list[NotifierSpec] = Field(default_factory=list)
    channels: list[RawChannel] = Field(default_factory=list)
    summary: SummarySpec = Field(default_factory=SummarySpec)


def load_config(path: Path) -> AppConfig:
    """Parse a YAML config file into a validated AppConfig."""
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


def merge_channel(raw: RawChannel, defaults: Defaults) -> ChannelConfig:
    """Combine a RawChannel with global defaults into a ChannelConfig."""
    return ChannelConfig(
        platform=raw.platform,
        channel_id=raw.channel_id,
        notifier_id=raw.notifier,
        poll_interval=raw.poll_interval if raw.poll_interval is not None else defaults.poll_interval,
        cookies_from_browser=raw.cookies_from_browser,
        save_dir=raw.save_dir if raw.save_dir is not None else defaults.save_dir,
        format=raw.format,
        alias=raw.alias,
        downloader=raw.downloader,
    )
