"""Tests for LiveRetryPolicy — bounds the orchestrator's re-recording of a
broadcast that is still live but keeps failing to download (e.g. an unpullable
stream), so it doesn't re-attempt every poll forever. The first retry stays
free (preserves "retry on next poll"); repeated failures back off exponentially.
"""
from linkstart.models import ChannelConfig
from linkstart.retry import ExponentialBackoffRetryPolicy


def _ch():
    return ChannelConfig(platform="twitcasting", channel_id="abc")


def test_first_attempt_for_unseen_live_is_allowed():
    p = ExponentialBackoffRetryPolicy(base_delay=30, factor=2, max_delay=600)
    assert p.should_attempt(_ch(), "L1", now=0.0) is True


def test_first_retry_is_free_after_one_failure():
    # Preserves the existing "a failed download is retried on the next poll".
    p = ExponentialBackoffRetryPolicy(base_delay=30, factor=2)
    ch = _ch()
    p.record_failure(ch, "L1", now=0.0)
    assert p.should_attempt(ch, "L1", now=0.0) is True


def test_backs_off_after_second_consecutive_failure():
    p = ExponentialBackoffRetryPolicy(base_delay=30, factor=2)
    ch = _ch()
    p.record_failure(ch, "L1", now=0.0)   # failures=1 → next retry free
    p.record_failure(ch, "L1", now=0.0)   # failures=2 → delay = base (30)
    assert p.should_attempt(ch, "L1", now=10.0) is False
    assert p.should_attempt(ch, "L1", now=30.0) is True


def test_backoff_grows_exponentially_and_caps():
    p = ExponentialBackoffRetryPolicy(base_delay=30, factor=2, max_delay=100)
    ch = _ch()
    p.record_failure(ch, "L1", now=0.0)
    p.record_failure(ch, "L1", now=0.0)   # failures=2 → 30
    assert p.should_attempt(ch, "L1", now=29.0) is False
    p.record_failure(ch, "L1", now=0.0)   # failures=3 → 60
    assert p.should_attempt(ch, "L1", now=59.0) is False
    assert p.should_attempt(ch, "L1", now=60.0) is True
    for _ in range(10):                   # far past the cap
        p.record_failure(ch, "L1", now=0.0)
    assert p.should_attempt(ch, "L1", now=99.0) is False
    assert p.should_attempt(ch, "L1", now=100.0) is True   # capped at max_delay


def test_success_resets_the_backoff():
    p = ExponentialBackoffRetryPolicy(base_delay=30)
    ch = _ch()
    for _ in range(3):
        p.record_failure(ch, "L1", now=0.0)
    p.record_success(ch, "L1")
    assert p.should_attempt(ch, "L1", now=0.0) is True


def test_distinct_live_ids_are_independent():
    p = ExponentialBackoffRetryPolicy(base_delay=30)
    ch = _ch()
    for _ in range(3):
        p.record_failure(ch, "L1", now=0.0)
    # A different broadcast on the same channel starts clean.
    assert p.should_attempt(ch, "L2", now=0.0) is True


def test_distinct_channels_are_independent():
    p = ExponentialBackoffRetryPolicy(base_delay=30)
    ch1 = ChannelConfig(platform="twitcasting", channel_id="abc")
    ch2 = ChannelConfig(platform="twitcasting", channel_id="xyz")
    for _ in range(3):
        p.record_failure(ch1, "L1", now=0.0)
    assert p.should_attempt(ch2, "L1", now=0.0) is True
