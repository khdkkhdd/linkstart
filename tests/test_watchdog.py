from linkstart.downloader._stall import NoProgressStallPolicy
from linkstart.downloader._watchdog import (
    Heartbeat, RecordingLabel, StallWatchdog, scan_output,
)


def test_scan_output_single_pass_total_and_largest(tmp_path):
    (tmp_path / "out.ts.part").write_bytes(b"x" * 100)
    (tmp_path / "out.f140.mp4").write_bytes(b"y" * 250)
    total, largest = scan_output(tmp_path / "out")
    assert total == 350
    assert largest == tmp_path / "out.f140.mp4"


def test_scan_output_empty_dir_returns_zero_none(tmp_path):
    total, largest = scan_output(tmp_path / "nothing")
    assert total == 0
    assert largest is None


def test_recording_label_renders():
    lbl = RecordingLabel(platform="youtube", log_name="ch", loop_name="full")
    assert str(lbl) == "youtube/ch [full]"


async def test_stall_watchdog_returns_when_policy_aborts(tmp_path):
    out = tmp_path / "out"
    (tmp_path / "out.ts.part").write_bytes(b"x")  # never grows again
    policy = NoProgressStallPolicy(stall_sec=0.05, grace_sec=0.0, poll_sec=0.01)
    wd = StallWatchdog(out, policy)
    await wd.watch()  # completing == "abort"; returns promptly without external growth
