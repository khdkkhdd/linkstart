"""On-disk path & name policy for recordings — where output lands and how it's named."""
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from linkstart.models import ChannelConfig, LiveInfo

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[\/\\:*?"<>|\x00-\x1f]')
_MAX_TITLE_LEN = 80


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


class RecordingPaths:
    """Decides where on disk (and under what name) a broadcast's output lands."""

    def make_parts_dir(self, channel: ChannelConfig, live: LiveInfo) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        safe_title = sanitize_title(live.title or live.live_id)
        out_dir = channel.save_dir / channel.platform / channel.display_name
        out_dir.mkdir(parents=True, exist_ok=True)
        parts_dir = out_dir / f"{date}_{safe_title}.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        return parts_dir

    def final_path(self, channel: ChannelConfig, live: LiveInfo) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        safe_title = sanitize_title(live.title or live.live_id)
        out_dir = channel.save_dir / channel.platform / channel.display_name
        return out_dir / f"{date}_{safe_title}.mp4"

    def discard_parts_dir(self, parts_dir: Path) -> None:
        """Remove a parts dir on a failure path, logging discarded bytes."""
        if not parts_dir.exists():
            return
        discarded = sum(
            p.stat().st_size for p in parts_dir.rglob("*") if p.is_file()
        )
        shutil.rmtree(parts_dir, ignore_errors=True)
        if discarded:
            log.debug(
                "discarded parts dir %s (%d bytes of incomplete output)",
                parts_dir, discarded,
            )
