"""Tests for StallPolicy — decides when a running download that makes no
progress should be aborted so the loop can retry (or give up) instead of
hanging for hours on a stream it can't actually pull (e.g. fMP4 remux that
only writes the init segment)."""
from linkstart.downloader._stall import (
    AnyStallPolicy,
    MinThroughputStallPolicy,
    NeverAbortStallPolicy,
    NoProgressStallPolicy,
)


def test_never_abort_policy_never_aborts():
    p = NeverAbortStallPolicy()
    assert p.should_abort(elapsed=10_000, bytes_written=0, since_growth=10_000) is False


def test_no_progress_within_grace_never_aborts():
    p = NoProgressStallPolicy(stall_sec=90, grace_sec=30)
    # Still inside the grace window — slow starts must not be killed.
    assert p.should_abort(elapsed=10, bytes_written=0, since_growth=10) is False


def test_no_progress_aborts_after_stall_window():
    p = NoProgressStallPolicy(stall_sec=90, grace_sec=30)
    # Past grace, and nothing has grown for >= stall_sec.
    assert p.should_abort(elapsed=200, bytes_written=56_000, since_growth=95) is True


def test_no_progress_keeps_running_while_growing():
    p = NoProgressStallPolicy(stall_sec=90, grace_sec=30)
    # Past grace but output grew recently → keep going.
    assert p.should_abort(elapsed=200, bytes_written=5_000_000, since_growth=3) is False


def test_poll_sec_default_and_override():
    assert NeverAbortStallPolicy().poll_sec > 0
    assert NoProgressStallPolicy(stall_sec=90, poll_sec=2.0).poll_sec == 2.0


# --- MinThroughputStallPolicy: catches a "trickle" that NoProgress misses ---

def test_min_throughput_within_window_never_aborts():
    # Below-threshold rate is tolerated until the evaluation window passes, so a
    # slow-starting download (manifest resolution, first segment) is not killed.
    p = MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60)
    assert p.should_abort(elapsed=30, bytes_written=0, since_growth=30) is False


def test_min_throughput_aborts_when_rate_below_threshold_after_window():
    # The real bug: ffmpeg trickled 27 KB over 44 min (~10 B/s) while the
    # zero-growth watchdog never fired because the file kept inching up.
    p = MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60)
    assert p.should_abort(elapsed=2640, bytes_written=27_824, since_growth=5) is True


def test_min_throughput_keeps_running_when_rate_healthy():
    # A real stream does hundreds of KB/s — comfortably above the floor.
    p = MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60)
    assert p.should_abort(elapsed=120, bytes_written=50_000_000, since_growth=1) is False


def test_min_throughput_just_past_window_with_low_total_aborts():
    # Right after the window with almost nothing written → abort.
    p = MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60)
    assert p.should_abort(elapsed=61, bytes_written=500, since_growth=2) is True


# --- AnyStallPolicy: compose zero-growth + min-throughput ---

def test_any_stall_aborts_if_any_child_aborts():
    frozen = NoProgressStallPolicy(stall_sec=90, grace_sec=30)
    trickle = MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60)
    p = AnyStallPolicy([frozen, trickle])
    # Healthy for zero-growth (grew recently) but a trickle by throughput.
    assert p.should_abort(elapsed=2640, bytes_written=27_824, since_growth=3) is True


def test_any_stall_does_not_abort_when_no_child_aborts():
    frozen = NoProgressStallPolicy(stall_sec=90, grace_sec=30)
    trickle = MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60)
    p = AnyStallPolicy([frozen, trickle])
    # Growing fast and recently → neither child aborts.
    assert p.should_abort(elapsed=120, bytes_written=50_000_000, since_growth=1) is False


def test_any_stall_poll_sec_is_min_of_children():
    p = AnyStallPolicy([
        NoProgressStallPolicy(stall_sec=90, poll_sec=5.0),
        MinThroughputStallPolicy(min_bytes_per_sec=1024, window_sec=60, poll_sec=2.0),
    ])
    assert p.poll_sec == 2.0
