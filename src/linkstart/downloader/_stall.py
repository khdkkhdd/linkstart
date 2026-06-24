"""Stall detection policies — injected per strategy to set per-context abort tolerances."""
from abc import ABC, abstractmethod


class StallPolicy(ABC):
    """Decides when a no-progress download should be aborted."""

    #: How often the watchdog should sample output growth, in seconds.
    poll_sec: float = 5.0

    @abstractmethod
    def should_abort(
        self, *, elapsed: float, bytes_written: int, since_growth: float
    ) -> bool:
        """Return True to abort the running download.

        elapsed:      seconds since the process started
        bytes_written: current total size of the output (incl. .part temp file)
        since_growth: seconds since the output last grew
        """


class NeverAbortStallPolicy(StallPolicy):
    """Never aborts — preserves pre-watchdog behavior."""

    def should_abort(
        self, *, elapsed: float, bytes_written: int, since_growth: float
    ) -> bool:
        return False


class NoProgressStallPolicy(StallPolicy):
    """Abort after ``stall_sec`` of zero output growth, past a ``grace_sec`` window."""

    def __init__(
        self, *, stall_sec: float, grace_sec: float = 0.0, poll_sec: float = 5.0
    ) -> None:
        self.stall_sec = stall_sec
        self.grace_sec = grace_sec
        self.poll_sec = poll_sec

    def should_abort(
        self, *, elapsed: float, bytes_written: int, since_growth: float
    ) -> bool:
        if elapsed < self.grace_sec:
            return False
        return since_growth >= self.stall_sec


class MinThroughputStallPolicy(StallPolicy):
    """Abort if average throughput stays below ``min_bytes_per_sec`` after ``window_sec``."""

    def __init__(
        self,
        *,
        min_bytes_per_sec: float,
        window_sec: float,
        poll_sec: float = 5.0,
    ) -> None:
        self.min_bytes_per_sec = min_bytes_per_sec
        self.window_sec = window_sec
        self.poll_sec = poll_sec

    def should_abort(
        self, *, elapsed: float, bytes_written: int, since_growth: float
    ) -> bool:
        if elapsed < self.window_sec or elapsed <= 0:
            return False
        return bytes_written < self.min_bytes_per_sec * elapsed


class AnyStallPolicy(StallPolicy):
    """Abort as soon as any child policy would; samples at the finest child ``poll_sec``."""

    def __init__(self, policies: list[StallPolicy]) -> None:
        if not policies:
            raise ValueError("AnyStallPolicy requires at least one policy")
        self.policies = policies
        self.poll_sec = min(p.poll_sec for p in policies)

    def should_abort(
        self, *, elapsed: float, bytes_written: int, since_growth: float
    ) -> bool:
        return any(
            p.should_abort(
                elapsed=elapsed,
                bytes_written=bytes_written,
                since_growth=since_growth,
            )
            for p in self.policies
        )
