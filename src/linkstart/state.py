"""Persistent JSON journal of the last live seen per channel.

Informational only (shown by `linkstart list`) — recording decisions never
depend on it. Assumes a single-writer process: concurrent writers may
silently lose updates since each instance keeps an in-memory copy and
overwrites the file on save without coordination.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def default_state_path() -> Path:
    """Default state location honoring XDG_STATE_HOME."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "linkstart" / "state.json"


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"channels": {}}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            # Corrupt state file: preserve it for debugging, then start fresh.
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time())}"
            )
            log.warning(
                "Corrupt state file at %s (%s); backing up to %s",
                self.path,
                exc,
                backup,
            )
            try:
                os.replace(self.path, backup)
            except OSError as rename_exc:
                log.warning(
                    "Failed to back up corrupt state file %s: %s",
                    self.path,
                    rename_exc,
                )
            return {"channels": {}}

    def _save(self) -> None:
        # Use a fixed predictable temp path so crashes don't leak random files.
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as tmp:
            json.dump(self._data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, self.path)

    @staticmethod
    def _key(platform: str, channel_id: str) -> str:
        return f"{platform}:{channel_id}"

    def mark_seen(self, platform: str, channel_id: str, live_id: str) -> None:
        self._data.setdefault("channels", {})[self._key(platform, channel_id)] = {
            "last_live_id": live_id,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_entry(self, platform: str, channel_id: str) -> dict | None:
        entry = self._data.get("channels", {}).get(self._key(platform, channel_id))
        return dict(entry) if entry else None
