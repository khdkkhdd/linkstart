"""Retry policy for re-recording a still-live broadcast that keeps failing.

When a broadcast is genuinely live but every download attempt fails (an
unpullable stream), the orchestrator's poll loop would otherwise re-attempt it
every poll interval for the broadcast's entire duration. `LiveRetryPolicy`
gates those re-attempts: the first retry stays free (so a one-off transient
failure is still retried promptly), and repeated consecutive failures back off
so the daemon stops hammering an unrecoverable stream.

The policy is an injected interface, not a branch in the orchestrator, so the
backoff curve can be swapped (or disabled) without touching the worker loop.
Time is passed in (`now`) rather than read internally, keeping the policy pure
and trivially testable.
"""
from abc import ABC, abstractmethod

from linkstart.models import ChannelConfig

_Key = tuple[str, str, str]


class LiveRetryPolicy(ABC):
    """Decides whether to (re-)attempt recording a given live broadcast."""

    @abstractmethod
    def should_attempt(
        self, channel: ChannelConfig, live_id: str, *, now: float
    ) -> bool:
        """Whether a recording attempt for this broadcast is allowed right now."""

    @abstractmethod
    def record_failure(
        self, channel: ChannelConfig, live_id: str, *, now: float
    ) -> None:
        """Note a failed recording attempt (advances the backoff)."""

    @abstractmethod
    def record_success(self, channel: ChannelConfig, live_id: str) -> None:
        """Note a successful recording (clears the backoff for this broadcast)."""


class AlwaysRetryPolicy(LiveRetryPolicy):
    """No backoff — re-attempt on every poll (the pre-policy behavior)."""

    def should_attempt(self, channel, live_id, *, now):
        return True

    def record_failure(self, channel, live_id, *, now):
        pass

    def record_success(self, channel, live_id):
        pass


class ExponentialBackoffRetryPolicy(LiveRetryPolicy):
    """Free first retry, then exponential backoff capped at ``max_delay``.

    Delay as a function of consecutive failures:
        f <= 1 : 0                              (retry on the next poll)
        f >= 2 : min(base_delay * factor**(f-2), max_delay)
    A success clears the broadcast's state. State is keyed per
    (platform, channel_id, live_id); a new broadcast starts clean.
    """

    def __init__(
        self, *, base_delay: float = 30.0, factor: float = 2.0, max_delay: float = 600.0
    ) -> None:
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        # key -> (consecutive_failures, last_attempt_time)
        self._state: dict[_Key, tuple[int, float]] = {}

    @staticmethod
    def _key(channel: ChannelConfig, live_id: str) -> _Key:
        return (channel.platform, channel.channel_id, live_id)

    def _delay(self, failures: int) -> float:
        if failures <= 1:
            return 0.0
        return min(self.base_delay * (self.factor ** (failures - 2)), self.max_delay)

    def should_attempt(self, channel, live_id, *, now):
        st = self._state.get(self._key(channel, live_id))
        if st is None:
            return True
        failures, last_attempt = st
        return (now - last_attempt) >= self._delay(failures)

    def record_failure(self, channel, live_id, *, now):
        key = self._key(channel, live_id)
        failures, _ = self._state.get(key, (0, 0.0))
        self._state[key] = (failures + 1, now)

    def record_success(self, channel, live_id):
        self._state.pop(self._key(channel, live_id), None)
