"""Stall detection and progress heartbeat for a running download."""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from linkstart.downloader._stall import StallPolicy

log = logging.getLogger(__name__)

WATCHDOG_POLL_SEC: float = 5.0
HEARTBEAT_INTERVAL_SEC: float = 30.0
HEARTBEAT_PROBE_TIMEOUT_SEC: float = 10.0


def scan_output(output: Path) -> tuple[int, Path | None]:
    """Return (total bytes, largest file) for all files matching `output.stem*`."""
    total = 0
    best: tuple[int, Path] | None = None
    for p in output.parent.glob(output.stem + "*"):
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        total += size
        if best is None or size > best[0]:
            best = (size, p)
    return total, (best[1] if best else None)


@dataclass(frozen=True)
class RecordingLabel:
    platform: str
    log_name: str
    loop_name: str

    def __str__(self) -> str:
        return f"{self.platform}/{self.log_name} [{self.loop_name}]"


class Heartbeat:
    """Periodic progress log (size + rate + best-effort content duration)."""

    def __init__(self, label: RecordingLabel, media,
                 interval_sec: float = HEARTBEAT_INTERVAL_SEC,
                 probe_timeout_sec: float = HEARTBEAT_PROBE_TIMEOUT_SEC) -> None:
        self.label = label
        self.media = media
        self.interval_sec = interval_sec
        self.probe_timeout_sec = probe_timeout_sec
        self._last_emit: float | None = None

    async def tick(self, *, now: float, start: float, total: int,
                   largest: Path | None, since_growth: float, poll_sec: float) -> None:
        if self._last_emit is None:
            self._last_emit = start
        if (now - self._last_emit) < self.interval_sec:
            return
        elapsed = now - start
        rate = total / elapsed if elapsed > 0 else 0.0
        note = f" — no growth {since_growth:.0f}s" if since_growth >= poll_sec else ""
        dur_note = ""
        try:
            secs = (await asyncio.wait_for(
                self.media.ffprobe_duration(largest), timeout=self.probe_timeout_sec
            )) if largest else None
            if secs:
                dur_note = f", {secs / 60:.0f}min content"
        except Exception:
            pass
        log.info("recording %s: %.1f MB%s in %.0fs (%.1f KB/s)%s",
                 self.label, total / 1e6, dur_note, elapsed, rate / 1024, note)
        self._last_emit = now


class StallWatchdog:
    """Polls output growth and returns once `policy` says to abort."""

    def __init__(self, output: Path, policy: StallPolicy,
                 heartbeat: Heartbeat | None = None) -> None:
        self.output = output
        self.policy = policy
        self.heartbeat = heartbeat

    async def watch(self) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        last_size = 0
        last_growth = start
        while True:
            await asyncio.sleep(self.policy.poll_sec)
            now = loop.time()
            total, largest = scan_output(self.output)
            if total > last_size:
                last_size = total
                last_growth = now
            if self.heartbeat is not None:
                await self.heartbeat.tick(
                    now=now, start=start, total=total, largest=largest,
                    since_growth=now - last_growth, poll_sec=self.policy.poll_sec,
                )
            if self.policy.should_abort(
                elapsed=now - start, bytes_written=total,
                since_growth=now - last_growth,
            ):
                return
