"""Time-window gate shared by rate-limited notifications."""
import time


class Cooldown:
    def __init__(self, window_sec: float) -> None:
        self.window_sec = window_sec
        self._last_at: float | None = None

    def try_acquire(self) -> bool:
        now = time.monotonic()
        if self._last_at is not None and (now - self._last_at) < self.window_sec:
            return False
        self._last_at = now
        return True
