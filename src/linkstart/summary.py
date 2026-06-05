"""Daily summary: append per-recording JSONL, fire summaries on cron."""
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from croniter import croniter

from linkstart.models import ChannelConfig, Event, EventType
from linkstart.notifier.base import Notifier
from linkstart.state import default_state_path

log = logging.getLogger(__name__)


def _log_path() -> Path:
    return default_state_path().parent / "recordings.jsonl"


def append_recording_record(
    *,
    platform: str,
    channel_id: str,
    file_path: Path,
    size_bytes: int,
    duration_sec: int,
) -> None:
    """Append a single recording event to the JSONL log."""
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "channel_id": channel_id,
        "file_path": str(file_path),
        "size_bytes": size_bytes,
        "duration_sec": duration_sec,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_records_since(since: datetime) -> list[dict]:
    path = _log_path()
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = datetime.fromisoformat(rec["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts >= since:
            out.append(rec)
    return out


def build_summary_text(records: list[dict]) -> str:
    if not records:
        return "No recordings in this window."
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        grouped[(r["platform"], r["channel_id"])].append(r)

    lines = []
    total_size = 0
    for (platform, channel_id), recs in sorted(grouped.items()):
        size = sum(r.get("size_bytes", 0) for r in recs)
        total_size += size
        lines.append(
            f"• {platform}/{channel_id}: {len(recs)} recordings, "
            f"{_human_size(size)}"
        )
    lines.append("")
    lines.append(f"Total: {len(records)} recordings, {_human_size(total_size)}")
    return "\n".join(lines)


def _human_size(b: int) -> str:
    val = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} PB"


def next_fire_time(cron_expr: str, now: datetime | None = None) -> datetime:
    base = now or datetime.now()
    itr = croniter(cron_expr, base)
    return itr.get_next(datetime)


async def run_summary_loop(
    cron_expr: str,
    notifier: Notifier,
    sample_channel: ChannelConfig,
    stop_event: asyncio.Event,
) -> None:
    """Background task: sleeps until next cron fire, then sends a daily summary."""
    while not stop_event.is_set():
        next_fire = next_fire_time(cron_expr)
        delay = max(0.01, (next_fire - datetime.now()).total_seconds())
        log.info("next summary at %s (in %.0fs)", next_fire.isoformat(), delay)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass

        since = datetime.now(timezone.utc) - timedelta(hours=24)
        records = load_records_since(since)
        text = build_summary_text(records)
        try:
            await notifier.notify(
                Event(
                    type=EventType.SUMMARY,
                    channel=sample_channel,
                    message=text,
                )
            )
        except Exception:
            log.exception("summary notify failed")
