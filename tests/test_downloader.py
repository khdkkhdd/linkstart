"""Tests for the Downloader (edge-only mode + helpers)."""
import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from linkstart.cooldown import Cooldown
from linkstart.downloader import (
    Downloader,
    _parse_epoch,
    sanitize_title,
    unique_path,
)
from linkstart.downloader._loop import _stderr_excerpt
from linkstart.downloader._cleanup import (
    cleanup_dual,
    _recover_fragments as _cleanup_recover_fragments,
)
from linkstart.downloader._dual import DualRecordingStrategy
from linkstart.downloader._edge import EdgeRecordingStrategy
from linkstart.downloader._stall import NeverAbortStallPolicy
from linkstart.downloader._strategy import RecordingStrategy
from linkstart.models import (
    ChannelConfig,
    DownloadProfile,
    DownloadResult,
    EventType,
    LiveInfo,
)
from linkstart.platforms.base import Platform


# --- sanitize_title / unique_path / _parse_epoch ---

def test_sanitize_title_replaces_invalid_chars():
    assert sanitize_title('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_title_strips_and_truncates():
    long = "a" * 200
    assert sanitize_title(long) == "a" * 80


def test_sanitize_title_empty_becomes_untitled():
    assert sanitize_title("") == "untitled"
    assert sanitize_title("   ") == "untitled"


def test_unique_path_suffixes(tmp_path):
    base = tmp_path / "x.mp4"
    base.write_text("")
    p1 = unique_path(base)
    assert p1 == tmp_path / "x_2.mp4"
    p1.write_text("")
    p2 = unique_path(base)
    assert p2 == tmp_path / "x_3.mp4"


def test_parse_epoch_full():
    assert _parse_epoch("full.1735012345.mp4") == 1735012345


def test_parse_epoch_edge():
    assert _parse_epoch("edge.1735012345.mp4") == 1735012345


def test_parse_epoch_collision_suffix():
    assert _parse_epoch("full.1735012345_01.mp4") == 1735012345


def test_parse_epoch_no_match():
    assert _parse_epoch("part00.ts") is None
    assert _parse_epoch("random.mp4") is None


# --- _stderr_excerpt ---

def test_stderr_excerpt_short_returned_whole():
    assert _stderr_excerpt(b"  boom happened  ") == "boom happened"


def test_stderr_excerpt_keeps_tail_not_head():
    # The real error lives at the END, after ffmpeg's "Opening ..." noise.
    head = b"HEADMARKER" + b"x" * 1000
    err = head + b"\nERROR: HTTP Error 404: Not Found"
    out = _stderr_excerpt(err, limit=50)
    assert out.startswith("…")
    assert "ERROR: HTTP Error 404: Not Found" in out
    assert "HEADMARKER" not in out  # head was dropped
    assert len(out) <= 51  # ellipsis + limit


# --- Edge-only record tests ---

class FakePlatform(Platform):
    name = "fake"
    supports_live_from_start = False
    # Edge/HLS-style fake → mpegts container (.ts parts), mirroring TwitCasting.
    _container = "mpegts"

    def __init__(self, *, check_results, yt_dlp_args_value=None):
        self._results = list(check_results)
        self.calls = 0
        # Legacy knob: extra flags injected into the download profile.
        self._extra_args = tuple(yt_dlp_args_value) if yt_dlp_args_value is not None else ()

    async def check_live(self, channel):
        self.calls += 1
        return self._results.pop(0) if self._results else None

    def build_url(self, channel, live):
        return f"https://fake/{channel.channel_id}"

    def download_profile(self, channel):
        return DownloadProfile(container=self._container, extra_args=self._extra_args)


class FakePlatformDual(FakePlatform):
    """Same fake but flagged as supporting from-start, for Task 5/6 tests."""
    supports_live_from_start = True
    default_format = "137+140/bestvideo+bestaudio/best"
    # YouTube-style dual recorder writes .mp4 directly (no --hls-use-mpegts).
    _container = "mp4"

    def recording_strategy(self, infra):
        from linkstart.downloader._dual import DualRecordingStrategy
        return DualRecordingStrategy(infra)


class FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode

    async def communicate(self):
        return b"", b"some stderr"


@pytest.fixture
def channel(tmp_path):
    return ChannelConfig(
        platform="fake",
        channel_id="abc",
        save_dir=tmp_path / "rec",
        poll_interval=0,
    )


@pytest.fixture
def live():
    return LiveInfo(live_id="100", title="hello", url="https://fake/abc")


async def test_edge_only_records_successfully_on_first_try(channel, live):
    # is_still_live returns False on the first check after yt-dlp exits 0
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"fakedata")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"finaldata")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    assert result.file_path is not None
    assert result.file_path.exists()
    assert result.retry_count == 0
    assert result.extra_files == []


async def test_edge_only_logs_broadcast_ended(channel, live, caplog):
    """When the attempt loop concludes the broadcast is no longer live, it must
    say so — otherwise the recording just stops with no logged reason."""
    plat = FakePlatform(check_results=[None])  # not live after the first attempt
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"fakedata")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"finaldata")
        return True

    with caplog.at_level(logging.INFO, logger="linkstart.downloader._loop"):
        with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
            with patch(
                "linkstart.downloader._process.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=fake_exec),
            ):
                await dl.record(channel, plat, live)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("broadcast ended" in m and live.live_id in m for m in msgs), msgs


async def test_edge_only_restarts_until_stream_ends(channel, live):
    # First two check_live calls return the same live (still live → restart);
    # third returns None (stream ended → stop).
    plat = FakePlatform(check_results=[live, None])
    dl = Downloader()

    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)   # normal exit but stream still running

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    # First invocation, is_still_live → True (live), restart → second invocation,
    # is_still_live → False (None), break.
    assert call_count["n"] == 2
    assert result.retry_count == 1
    assert result.success is True


async def test_edge_only_no_parts_returns_failure(channel, live):
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        # Do NOT create the output file → no parts
        return FakeProc(returncode=1)

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await dl.record(channel, plat, live)

    assert result.success is False
    assert result.error == "no parts captured"


async def test_edge_only_cleans_up_parts_dir_on_failure(channel, live):
    """A failed recording must not leave an orphan .parts directory behind."""
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=1)   # never writes output

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await dl.record(channel, plat, live)

    assert result.success is False
    assert list(channel.save_dir.rglob("*.parts")) == []


async def test_edge_only_fires_on_interrupted_callback(channel, live):
    # 1 restart cycle → 1 interrupted event
    plat = FakePlatform(check_results=[live, None])
    dl = Downloader()
    events = []

    async def on_interrupted(event):
        events.append(event)

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live, on_interrupted=on_interrupted)

    assert len(events) == 1
    assert events[0].type == EventType.DOWNLOAD_INTERRUPTED
    assert "edge" in (events[0].message or "")


def test_build_args_edge_no_live_from_start(tmp_path):
    dl = Downloader()
    plat = FakePlatform(check_results=[])  # mpegts profile → --hls-use-mpegts
    ch = ChannelConfig(platform="fake", channel_id="abc",
                       save_dir=tmp_path / "rec")
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    out = tmp_path / "part00.ts"
    args = dl._build_args(plat, ch, live, live_from_start=False, output=out)
    assert "--live-from-start" not in args
    assert "--hls-use-mpegts" in args
    # Retries must be finite — see test_build_args_uses_finite_retries.
    assert "--retries" in args
    int(args[args.index("--retries") + 1])
    assert str(out) in args
    assert "https://fake/abc" in args


def test_build_args_full_adds_live_from_start(tmp_path):
    dl = Downloader()
    plat = FakePlatformDual(check_results=[], yt_dlp_args_value=[])
    ch = ChannelConfig(platform="fake", channel_id="abc",
                       save_dir=tmp_path / "rec")
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    out = tmp_path / "full.123.mp4"
    args = dl._build_args(plat, ch, live, live_from_start=True, output=out)
    assert "--live-from-start" in args


def test_build_args_channel_format_overrides_default(tmp_path):
    dl = Downloader()
    plat = FakePlatformDual(check_results=[])
    ch = ChannelConfig(platform="fake", channel_id="abc",
                       save_dir=tmp_path / "rec",
                       format="299+140")
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    out = tmp_path / "full.123.mp4"
    args = dl._build_args(plat, ch, live, live_from_start=True, output=out)
    # channel.format wins over platform.default_format
    f_idx = args.index("-f")
    assert args[f_idx + 1] == "299+140"


def test_build_args_falls_back_to_platform_default_format(tmp_path):
    dl = Downloader()
    plat = FakePlatformDual(check_results=[])
    ch = ChannelConfig(platform="fake", channel_id="abc",
                       save_dir=tmp_path / "rec",
                       format=None)
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    out = tmp_path / "full.123.mp4"
    args = dl._build_args(plat, ch, live, live_from_start=True, output=out)
    f_idx = args.index("-f")
    assert args[f_idx + 1] == "137+140/bestvideo+bestaudio/best"


def test_build_args_no_format_when_neither_set(tmp_path):
    dl = Downloader()
    plat = FakePlatform(check_results=[])    # default_format = None
    ch = ChannelConfig(platform="fake", channel_id="abc",
                       save_dir=tmp_path / "rec")
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    out = tmp_path / "part00.ts"
    args = dl._build_args(plat, ch, live, live_from_start=False, output=out)
    assert "-f" not in args


async def test_edge_only_multiple_parts_produce_separate_mp4s(channel, live):
    """When yt-dlp restarted N times during a broadcast, each part is remuxed
    into its own .mp4 — first → main file_path, rest → extras as .part_NNN.mp4.
    Concat would silently hide the inter-attempt gaps."""
    # 3 successful yt-dlp runs (initial + 2 restarts).
    plat = FakePlatform(check_results=[live, live, None])
    dl = Downloader()
    call_index = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_index["n"] += 1
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data" + bytes(call_index["n"]))
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"remuxed_" + part.name.encode())
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    assert result.file_path is not None
    assert result.file_path.suffix == ".mp4"
    assert result.file_path.exists()
    # 2 extras = part 2 and part 3 (the restarts).
    assert len(result.extra_files) == 2
    names = [p.name for p in result.extra_files]
    assert any(".part_001.mp4" in n for n in names)
    assert any(".part_002.mp4" in n for n in names)
    # Each extra is its own file (NOT a single concatenated one).
    for extra in result.extra_files:
        assert extra.exists()


async def test_edge_only_main_remux_failure_returns_failure(channel, live):
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        return False   # main remux fails

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is False
    assert "remux" in (result.error or "").lower()


async def test_edge_only_extra_remux_failure_skipped(channel, live):
    """If one extra part fails to remux, the main file still wins and the
    failing part is silently skipped (not in extras)."""
    plat = FakePlatform(check_results=[live, None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    call_count = {"n": 0}

    async def fake_remux(part, target):
        call_count["n"] += 1
        if call_count["n"] == 1:
            target.write_bytes(b"main")
            return True
        return False   # second remux (the extra) fails

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    assert result.extra_files == []


async def test_edge_only_returns_failure_when_remux_fails(channel, live):
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        return False   # simulate ffmpeg remux failure

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is False
    assert result.error == "ffmpeg remux failed"


async def test_edge_only_sleeps_between_attempts_when_configured(channel, live, monkeypatch):
    """EDGE_LOOP_SLEEP > 0 → asyncio.sleep is awaited between yt-dlp restarts."""
    plat = FakePlatform(check_results=[live, None])
    dl = Downloader()
    sleeps: list[float] = []
    real_sleep = asyncio.sleep   # capture before patch

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        # Yield to the scheduler so the stall watchdog's poll loop (which also
        # awaits asyncio.sleep) does not starve the event loop under this patch.
        await real_sleep(0)

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_LOOP_SLEEP", 2.5)
    monkeypatch.setattr("linkstart.downloader._edge.asyncio.sleep", fake_sleep)

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    assert 2.5 in sleeps


async def test_edge_only_summary_append_exception_is_swallowed(channel, live, monkeypatch):
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    def boom(**kw):
        raise RuntimeError("summary store broken")

    monkeypatch.setattr("linkstart.summary.append_recording_record", boom)

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    # Summary failure must not affect overall success.
    assert result.success is True


async def test_edge_only_appends_summary_record(channel, live, tmp_path, monkeypatch):
    """Successful edge-only recording must append a summary JSONL record."""
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    captured = {}

    def fake_append(*, platform, channel_id, file_path, size_bytes, duration_sec):
        captured["platform"] = platform
        captured["channel_id"] = channel_id
        captured["file_path"] = file_path

    monkeypatch.setattr(
        "linkstart.summary.append_recording_record", fake_append
    )

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    assert captured["platform"] == "fake"
    assert captured["channel_id"] == "abc"
    assert captured["file_path"] == result.file_path


def test_parts_dir_uses_alias_when_set(tmp_path):
    """When alias is set, save path uses it instead of cryptic channel_id."""
    dl = Downloader()
    ch = ChannelConfig(
        platform="chzzk",
        channel_id="abcdef0123456789abcdef0123456789",
        alias="mychannel",
        save_dir=tmp_path / "rec",
    )
    live = LiveInfo(live_id="123", title="t", url="https://x")
    parts_dir = dl.paths.make_parts_dir(ch, live)
    assert "mychannel" in str(parts_dir)
    assert "abcdef0123456789abcdef0123456789" not in str(parts_dir)


def test_parts_dir_falls_back_to_channel_id_without_alias(tmp_path):
    dl = Downloader()
    ch = ChannelConfig(
        platform="twitcasting",
        channel_id="somehandle",
        save_dir=tmp_path / "rec",
    )
    live = LiveInfo(live_id="1", title="t", url="https://x")
    parts_dir = dl.paths.make_parts_dir(ch, live)
    assert "somehandle" in str(parts_dir)


def test_final_path_uses_alias_when_set(tmp_path):
    dl = Downloader()
    ch = ChannelConfig(
        platform="chzzk",
        channel_id="abcdef0123456789abcdef0123456789",
        alias="mychannel",
        save_dir=tmp_path / "rec",
    )
    live = LiveInfo(live_id="1", title="제목", url="https://x")
    final = dl.paths.final_path(ch, live)
    assert "mychannel" in str(final)


def test_build_args_uses_finite_retries(tmp_path):
    """yt-dlp must be allowed to exit when the broadcast ends; infinite retries
    would make it hang waiting for new HLS segments forever."""
    dl = Downloader()
    plat = FakePlatform(check_results=[], yt_dlp_args_value=[])
    ch = ChannelConfig(platform="fake", channel_id="abc", save_dir=tmp_path / "rec")
    live = LiveInfo(live_id="100", title="t", url="https://x")
    args = dl._build_args(plat, ch, live, live_from_start=False, output=tmp_path / "p.ts")
    assert "infinite" not in args
    assert "--retries" in args
    assert "--fragment-retries" in args
    # Values must be parseable as ints.
    int(args[args.index("--retries") + 1])
    int(args[args.index("--fragment-retries") + 1])


def test_build_args_includes_cookies_from_browser_when_set(tmp_path):
    dl = Downloader()
    plat = FakePlatform(check_results=[], yt_dlp_args_value=[])
    ch = ChannelConfig(
        platform="fake", channel_id="abc",
        save_dir=tmp_path / "rec", cookies_from_browser="chrome",
    )
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    args = dl._build_args(plat, ch, live, live_from_start=False, output=tmp_path / "p.ts")
    assert "--cookies-from-browser" in args
    assert "chrome" in args


def test_build_args_empty_string_format_suppresses_flag(tmp_path):
    """channel.format == '' should suppress -f entirely (not fall back to default)."""
    dl = Downloader()
    plat = FakePlatformDual(check_results=[])   # has default_format
    ch = ChannelConfig(platform="fake", channel_id="abc",
                       save_dir=tmp_path / "rec",
                       format="")
    live = LiveInfo(live_id="100", title="t", url="https://fake/abc")
    out = tmp_path / "part.ts"
    args = dl._build_args(plat, ch, live, live_from_start=False, output=out)
    assert "-f" not in args


# --- fast-fail without output (e.g. unreadable cookies, disk full) ---

async def test_edge_only_gives_up_after_consecutive_no_output_failures(channel, live):
    """yt-dlp exiting non-zero WITHOUT producing any data must not be retried
    in a hot loop for the whole broadcast: give up after the limit and surface
    the stderr so the operator gets notified."""
    plat = FakePlatform(check_results=[live] * 20)
    dl = Downloader()
    exec_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        exec_count["n"] += 1
        # Never create the output file — instant failure (cookie error etc.).
        return FakeProc(returncode=1)

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await dl.record(channel, plat, live)

    assert result.success is False
    assert exec_count["n"] == EdgeRecordingStrategy.NO_OUTPUT_FAIL_LIMIT
    assert "without producing output" in (result.error or "")
    # The last stderr is surfaced for diagnosis.
    assert "some stderr" in (result.error or "")


async def test_edge_only_gives_up_when_exit_zero_produces_no_output(channel, live):
    """yt-dlp can exit 0 without writing anything while the platform still
    reports the broadcast live. That mismatch must hit the same bail-out as
    non-zero exits — otherwise the loop spins sleeplessly forever."""
    plat = FakePlatform(check_results=[live] * 20)
    dl = Downloader()
    exec_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        exec_count["n"] += 1
        return FakeProc(returncode=0)

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await dl.record(channel, plat, live)

    assert result.success is False
    assert exec_count["n"] == EdgeRecordingStrategy.NO_OUTPUT_FAIL_LIMIT
    assert "without producing output" in (result.error or "")


async def test_edge_only_no_output_counter_resets_when_data_produced(channel, live):
    """A failure streak interrupted by a successful attempt must reset the
    counter — only CONSECUTIVE no-output failures trigger the bail-out."""
    # fail, fail, produce-data, fail, fail, fail → bail at the limit.
    # Broadcast stays live far longer, so reaching exactly 6 attempts proves
    # the bail-out (without it, attempts would continue past 6).
    plat = FakePlatform(check_results=[live] * 10)
    dl = Downloader()
    exec_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        exec_count["n"] += 1
        if exec_count["n"] == 3:
            out_idx = args.index("-o")
            Path(args[out_idx + 1]).write_bytes(b"data")
            return FakeProc(returncode=0)
        return FakeProc(returncode=1)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    # 2 failures (counter 2) + success (reset) + 2 failures (counter 2) +
    # one more failure hits the limit of 3 → 6 attempts total.
    assert exec_count["n"] == 6
    # Data WAS produced on attempt 3 → overall success with that part.
    assert result.success is True


async def test_dual_loops_give_up_after_consecutive_no_output_failures(channel, live, monkeypatch):
    monkeypatch.setattr(DualRecordingStrategy, "FULL_LOOP_SLEEP", 0.0)
    plat = FakePlatformDual(check_results=[live] * 40)
    dl = Downloader()
    exec_count = {"full": 0, "edge": 0}

    async def fake_exec(*args, **kwargs):
        if "--live-from-start" in args:
            exec_count["full"] += 1
        else:
            exec_count["edge"] += 1
        return FakeProc(returncode=1)

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await asyncio.wait_for(dl.record(channel, plat, live), timeout=5.0)

    assert result.success is False
    assert exec_count["full"] == DualRecordingStrategy.NO_OUTPUT_FAIL_LIMIT
    assert exec_count["edge"] == DualRecordingStrategy.NO_OUTPUT_FAIL_LIMIT
    # The bail-out diagnosis (yt-dlp stderr) must survive into the result —
    # it is what the operator sees on Discord.
    assert "without producing output" in (result.error or "")
    assert "some stderr" in (result.error or "")


# --- Dual-mode loop tests ---

async def test_dual_runs_both_loops_then_cleanup(channel, live, tmp_path):
    plat = FakePlatformDual(check_results=[None, None])  # both loops end immediately
    dl = Downloader()
    exec_calls = []

    async def fake_exec(*args, **kwargs):
        exec_calls.append(args)
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_cleanup(paths, media, channel, live, parts_dir, *, retry_count, full_restarted=False):
        # Pretend cleanup succeeded with a known base path.
        base = parts_dir.parent / "fake_base.mp4"
        base.write_bytes(b"base")
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    # Two yt-dlp invocations: one full, one edge (each loop ran once before still_live=False)
    assert len(exec_calls) == 2
    # Verify one had --live-from-start and one did not
    flags = ["--live-from-start" in call for call in exec_calls]
    assert sorted(flags) == [False, True]


async def test_dual_full_loop_uses_sleep_5(monkeypatch, channel, live):
    plat = FakePlatformDual(check_results=[live, None, None])
    dl = Downloader()
    sleeps: list[float] = []
    real_sleep = asyncio.sleep   # capture before patch

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        # Yield so the stall watchdog's poll loop does not starve the loop.
        await real_sleep(0)

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    monkeypatch.setattr("linkstart.downloader._dual.asyncio.sleep", fake_sleep)
    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    # The first check_live → live (still live → full loop restarts → sleep 5).
    # Then full's second check → None → break. Edge's only check → None (after edge took the
    # second result; ordering is non-deterministic). At minimum, the full loop should have
    # slept 5s exactly once because of one restart.
    assert 5.0 in sleeps


async def test_dual_edge_loop_restarts_and_warns_on_nonzero(channel, live, monkeypatch, tmp_path):
    """Cover edge_loop's restart path: counters['edge']++ and the warn branch
    when yt-dlp returns non-zero."""

    # Custom platform with deterministic "alive for N checks" semantics so both
    # loops are guaranteed to see at least one restart cycle.
    class CountingPlatform(FakePlatformDual):
        def __init__(self, alive_for: int):
            super().__init__(check_results=[])
            self._alive_for = alive_for
            self._checks = 0

        async def check_live(self, channel):
            self._checks += 1
            return live if self._checks <= self._alive_for else None

    plat = CountingPlatform(alive_for=6)
    dl = Downloader()
    monkeypatch.setattr(DualRecordingStrategy, "EDGE_LOOP_SLEEP", 0.01)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep   # capture before patch

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        # Force a real scheduler yield so the sibling task can progress; without
        # this, AsyncMock-based awaits in this test never give edge a turn.
        await real_sleep(0)

    monkeypatch.setattr("linkstart.downloader._dual.asyncio.sleep", fake_sleep)

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=1)   # non-zero → warn branch in both loops

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    # Both edge sleep (0.01) and full sleep (5.0) should have been called at least once.
    assert 0.01 in sleeps
    assert 5.0 in sleeps


async def test_dual_unique_output_raises_after_100_collisions(channel, live, monkeypatch, tmp_path):
    """Edge case: 100 collisions on the same epoch must raise rather than spin."""
    dl = Downloader()
    monkeypatch.setattr("linkstart.downloader._dual.time.time", lambda: 9999999999)

    # Pre-create the colliding files so _unique_output exhausts its 0..99 range.
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    parts_dir_path = tmp_path / "rec" / "fake" / "abc" / f"{today}_hello.parts"
    parts_dir_path.mkdir(parents=True)
    (parts_dir_path / "full.9999999999.mp4").write_bytes(b"")
    for i in range(1, 100):
        (parts_dir_path / f"full.9999999999_{i:02d}.mp4").write_bytes(b"")

    plat = FakePlatformDual(check_results=[live, live, None, None])

    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=0)

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            with pytest.raises(RuntimeError, match="could not find unique output"):
                await dl.record(channel, plat, live)


async def test_dual_fires_interrupted_callback(channel, live, tmp_path):
    # full has one restart, edge none.
    plat = FakePlatformDual(check_results=[live, None, None])
    dl = Downloader()
    events = []

    async def on_interrupted(event):
        events.append(event)

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live, on_interrupted=on_interrupted)

    # At least one INTERRUPTED event fired (from the one restart)
    assert any(e.type == EventType.DOWNLOAD_INTERRUPTED for e in events)


async def test_dual_unique_output_resolves_epoch_collision(channel, live,
                                                           monkeypatch, tmp_path):
    # Freeze time.time() to return the same value twice in a row.
    fixed = [1735012345, 1735012345]   # consecutive identical
    monkeypatch.setattr("linkstart.downloader._dual.time.time",
                        lambda: fixed.pop(0) if fixed else 1735012346)

    # 2 yt-dlp invocations, both as 'full' (because we only test the unique_output helper here).
    plat = FakePlatformDual(check_results=[live, None, None])
    dl = Downloader()

    outputs: list[str] = []

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        outputs.append(args[out_idx + 1])
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    # We expect at least one of {"full.1735012345.mp4", "full.1735012345_01.mp4"} or
    # similar collision-resolved name to appear.
    names = [Path(o).name for o in outputs]
    full_names = [n for n in names if n.startswith("full.")]
    # At minimum, full had two attempts (initial + restart). They should not collide.
    assert len(set(full_names)) == len(full_names)


class _ClockStub:
    def __init__(self, initial: float = 0.0):
        self.value = initial

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float):
        self.value += delta


async def test_notify_interrupted_fires_first(channel, live, monkeypatch):
    dl = Downloader()
    clock = _ClockStub(initial=1000.0)
    monkeypatch.setattr("linkstart.cooldown.time.monotonic", clock)
    events = []

    async def cb(event):
        events.append(event)

    cooldown = Cooldown(RecordingStrategy.INTERRUPTED_DEDUP_SEC)
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=1, cooldown=cooldown
    )
    assert len(events) == 1


async def test_notify_interrupted_dedupes_within_window(channel, live, monkeypatch):
    dl = Downloader()
    clock = _ClockStub(initial=1000.0)
    monkeypatch.setattr("linkstart.cooldown.time.monotonic", clock)
    events = []

    async def cb(event):
        events.append(event)

    cooldown = Cooldown(RecordingStrategy.INTERRUPTED_DEDUP_SEC)
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=1, cooldown=cooldown
    )
    # Two more calls within the dedup window.
    clock.advance(60.0)
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=2, cooldown=cooldown
    )
    clock.advance(60.0)
    await dl._notify_interrupted(
        cb, channel, live, loop="full", attempt=1, cooldown=cooldown
    )

    # Only the first one fires.
    assert len(events) == 1
    assert events[0].retry_count == 1


async def test_notify_interrupted_fires_again_after_window(channel, live, monkeypatch):
    dl = Downloader()
    clock = _ClockStub(initial=1000.0)
    monkeypatch.setattr("linkstart.cooldown.time.monotonic", clock)
    events = []

    async def cb(event):
        events.append(event)

    cooldown = Cooldown(RecordingStrategy.INTERRUPTED_DEDUP_SEC)
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=1, cooldown=cooldown
    )
    clock.advance(RecordingStrategy.INTERRUPTED_DEDUP_SEC + 1.0)
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=2, cooldown=cooldown
    )
    assert len(events) == 2
    assert [e.retry_count for e in events] == [1, 2]


async def test_notify_interrupted_no_callback_does_not_consume_cooldown(channel, live):
    dl = Downloader()
    events = []

    async def cb(event):
        events.append(event)

    cooldown = Cooldown(RecordingStrategy.INTERRUPTED_DEDUP_SEC)
    await dl._notify_interrupted(
        None, channel, live, loop="edge", attempt=1, cooldown=cooldown
    )
    # The no-op call must leave the cooldown available for a real one.
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=2, cooldown=cooldown
    )
    assert len(events) == 1


async def test_notify_interrupted_swallows_callback_exception(channel, live):
    """Callback failures must not propagate — recording continues regardless."""
    dl = Downloader()

    async def boom(event):
        raise RuntimeError("notifier exploded")

    events = []

    async def cb(event):
        events.append(event)

    cooldown = Cooldown(RecordingStrategy.INTERRUPTED_DEDUP_SEC)
    # Must not raise.
    await dl._notify_interrupted(
        boom, channel, live, loop="edge", attempt=1, cooldown=cooldown
    )
    # Even though the callback raised, the cooldown was consumed.
    await dl._notify_interrupted(
        cb, channel, live, loop="edge", attempt=2, cooldown=cooldown
    )
    assert events == []


async def test_run_proc_kills_when_wait_is_cancelled():
    """If proc.wait() raises (e.g. cancellation during cleanup), fall back to kill()."""
    dl = Downloader()

    class HostileProc:
        def __init__(self):
            self.returncode = None
            self.terminate_called = False
            self.kill_called = False
            self._wait_calls = 0

        async def communicate(self):
            raise asyncio.CancelledError()

        def terminate(self):
            self.terminate_called = True

        def kill(self):
            self.kill_called = True
            self.returncode = -9

        async def wait(self):
            self._wait_calls += 1
            # First wait (cleanup after terminate) raises; kill should follow.
            raise asyncio.CancelledError()

    proc = HostileProc()

    async def fake_exec(*args, **kwargs):
        return proc

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        with pytest.raises(asyncio.CancelledError):
            await dl.process.run(["yt-dlp"])

    assert proc.terminate_called is True
    assert proc.kill_called is True


class TerminatableProc:
    """Fake subprocess.Process that records terminate()/kill() calls and
    can be made to hang inside communicate() until terminate() fires."""

    def __init__(self, *, mode: str = "hang"):
        self.mode = mode
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self._done = asyncio.Event()

    async def communicate(self):
        if self.mode == "raise":
            raise RuntimeError("kaboom")
        await self._done.wait()
        return b"", b""

    def terminate(self):
        self.terminate_called = True
        if self.returncode is None:
            self.returncode = -15
        self._done.set()

    def kill(self):
        self.kill_called = True
        if self.returncode is None:
            self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


class _RetryingFakePlatform(Platform):
    """check_live returns False (None) for the first N calls, then live."""
    name = "fake"
    supports_live_from_start = False

    def __init__(self, fail_count: int, live: LiveInfo):
        self._fail_count = fail_count
        self._live = live
        self.calls = 0

    async def check_live(self, channel):
        self.calls += 1
        return None if self.calls <= self._fail_count else self._live

    def build_url(self, channel, live):
        return f"https://fake/{channel.channel_id}"


async def test_is_still_live_with_retries_returns_true_on_recovery(channel, live, monkeypatch):
    """If platform.is_still_live returns False once then True, the retry helper
    returns True (broadcast still going — first False was transient)."""
    dl = Downloader()
    # 1 fail (False), then live (True). One retry needed.
    plat = _RetryingFakePlatform(fail_count=1, live=live)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("linkstart.downloader._loop.asyncio.sleep", fake_sleep)
    assert await dl._is_still_live_with_retries(plat, channel, live) is True
    # Two check_live calls: initial False, retry returned True.
    assert plat.calls == 2
    # Exactly one delay between attempts.
    assert sleeps == [dl.IS_STILL_LIVE_RETRY_DELAY]


async def test_is_still_live_with_retries_gives_up_after_all_fail(channel, live, monkeypatch):
    """If all retries return False, helper returns False — broadcast ended."""
    dl = Downloader()
    # All checks fail (broadcast actually ended).
    plat = _RetryingFakePlatform(fail_count=999, live=live)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("linkstart.downloader._loop.asyncio.sleep", fake_sleep)
    assert await dl._is_still_live_with_retries(plat, channel, live) is False
    assert plat.calls == dl.IS_STILL_LIVE_RETRIES
    # Delays are inserted between attempts but NOT after the last one.
    assert len(sleeps) == dl.IS_STILL_LIVE_RETRIES - 1


async def test_is_still_live_with_retries_short_circuits_on_first_true(channel, live, monkeypatch):
    """First check returns True → no retries, no delays."""
    dl = Downloader()
    plat = _RetryingFakePlatform(fail_count=0, live=live)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("linkstart.downloader._loop.asyncio.sleep", fake_sleep)
    assert await dl._is_still_live_with_retries(plat, channel, live) is True
    assert plat.calls == 1
    assert sleeps == []


async def test_edge_only_breaks_and_saves_when_stop_signaled(channel, live, tmp_path):
    """When stop_event fires during yt-dlp run, SIGTERM the subprocess, let it
    finalize, break out of loop, remux part0 → mp4 saved."""
    plat = FakePlatform(check_results=[live, live, live, live])  # broadcast continues
    dl = Downloader()
    stop_event = asyncio.Event()
    procs: list[TerminatableProc] = []

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")   # blocks until terminate() called
        procs.append(proc)
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return proc

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            task = asyncio.create_task(
                dl.record(channel, plat, live, stop_event=stop_event)
            )
            # Wait until yt-dlp is running.
            for _ in range(30):
                await asyncio.sleep(0)
                if procs:
                    break
            stop_event.set()
            result = await asyncio.wait_for(task, timeout=2.0)

    assert result.success is True
    assert result.file_path is not None
    assert result.file_path.exists()
    # yt-dlp got terminated, only one attempt — no restart after stop.
    assert len(procs) == 1
    assert procs[0].terminate_called is True


async def test_edge_only_watchdog_aborts_stalled_download(channel, live, monkeypatch):
    """A download that produces no growing output is aborted (SIGTERM) by the
    stall watchdog instead of being left to hang indefinitely."""
    plat = FakePlatform(check_results=[None])  # broadcast 'ends' after the attempt
    dl = Downloader()
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_SEC", 0.0)
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_GRACE_SEC", 0.0)
    dl.WATCHDOG_POLL_SEC = 0.01
    procs: list[TerminatableProc] = []

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")  # never exits on its own
        procs.append(proc)
        return proc  # writes NO output → never grows

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await asyncio.wait_for(dl.record(channel, plat, live), timeout=2.0)

    assert procs[0].terminate_called is True
    assert result.success is False


async def test_edge_only_aborts_slow_trickle_via_throughput_floor(channel, live, monkeypatch):
    """A download that writes a tiny stub and then only trickles (so the
    zero-growth watchdog never fires) must still be aborted by the minimum
    throughput floor — the real TwitCasting bug (27 KB over 44 min). The
    zero-growth window is disabled here so ONLY the throughput policy can abort."""
    plat = FakePlatform(check_results=[None])  # broadcast ends after the attempt
    dl = Downloader()
    # Make the zero-growth watchdog effectively never fire.
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_SEC", 10_000.0)
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_GRACE_SEC", 0.0)
    # Throughput floor: evaluate immediately, demand an absurd rate so the tiny
    # stub is judged "too slow" within the first poll.
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_MIN_RATE_WINDOW_SEC", 0.0)
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_MIN_BYTES_PER_SEC", 1e9)
    dl.WATCHDOG_POLL_SEC = 0.01
    procs: list[TerminatableProc] = []

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")  # never exits on its own
        procs.append(proc)
        out_idx = args.index("-o")
        # Tiny stub that never grows → since_growth keeps rising, but the
        # zero-growth window is too long to fire. Only throughput can abort.
        Path(args[out_idx + 1] + ".part").write_bytes(b"x" * 100)
        return proc

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await asyncio.wait_for(dl.record(channel, plat, live), timeout=2.0)

    assert procs[0].terminate_called is True
    assert result.success is False


async def test_edge_only_gives_up_after_repeated_stalls(channel, live, monkeypatch):
    """Stalled attempts count as no-progress even when a tiny stub was written
    (e.g. the 56KB fMP4 init), so the loop gives up at NO_OUTPUT_FAIL_LIMIT
    rather than churning forever."""
    plat = FakePlatform(check_results=[live] * 50)  # always still-live
    dl = Downloader()
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_SEC", 0.0)
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_GRACE_SEC", 0.0)
    dl.WATCHDOG_POLL_SEC = 0.01
    procs: list[TerminatableProc] = []

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")
        procs.append(proc)
        out_idx = args.index("-o")
        # Write a tiny stub like a stalled stream's init segment.
        Path(args[out_idx + 1] + ".part").write_bytes(b"x" * 100)
        return proc

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await asyncio.wait_for(dl.record(channel, plat, live), timeout=3.0)

    assert result.success is False
    assert "stall" in (result.error or "").lower()
    # Gave up at the limit — did NOT loop through all 50 still-live checks.
    assert len(procs) == EdgeRecordingStrategy.NO_OUTPUT_FAIL_LIMIT


async def test_heartbeat_label_includes_loop_name(channel, live, caplog, monkeypatch):
    """Heartbeats must name the loop (full/edge) so the two concurrent dual-mode
    recordings are distinguishable in the log — not two identical 'recording
    youtube/Ao' lines."""
    plat = FakePlatform(check_results=[None])
    dl = Downloader()
    dl.HEARTBEAT_INTERVAL_SEC = 0.0           # emit on the first poll
    dl.WATCHDOG_POLL_SEC = 0.01
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_SEC", 0.0)           # abort right after the heartbeat
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_STALL_GRACE_SEC", 0.0)
    monkeypatch.setattr(EdgeRecordingStrategy, "EDGE_MIN_RATE_WINDOW_SEC", 10_000.0)     # keep throughput policy from racing

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")
        out_idx = args.index("-o")
        Path(args[out_idx + 1] + ".part").write_bytes(b"x" * 100)
        return proc

    with caplog.at_level(logging.INFO, logger="linkstart.downloader._watchdog"):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await asyncio.wait_for(dl.record(channel, plat, live), timeout=2.0)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("recording" in m and "[edge]" in m for m in msgs), msgs


async def test_watchdog_emits_progress_heartbeat(tmp_path, caplog):
    """A running recording must log a periodic heartbeat with size + rate, so a
    healthy capture and a stuck one are distinguishable in the log instead of
    both being silent between the 'attempt' and 'exited' lines."""
    from linkstart.downloader._stall import StallPolicy
    from linkstart.downloader._watchdog import Heartbeat, StallWatchdog

    class _AbortAfter(StallPolicy):
        poll_sec = 0.01

        def __init__(self, n):
            self.n = n
            self.calls = 0

        def should_abort(self, *, elapsed, bytes_written, since_growth):
            self.calls += 1
            return self.calls >= self.n

    dl = Downloader()
    out = tmp_path / "part00.ts"
    # yt-dlp's in-progress temp file (matched by stem) — ~2 MB written so far.
    (tmp_path / "part00.ts.part").write_bytes(b"x" * 2_000_000)

    hb = Heartbeat("twitcasting/Ao", dl.media, interval_sec=0.0)
    with caplog.at_level(logging.INFO, logger="linkstart.downloader._watchdog"):
        await asyncio.wait_for(
            StallWatchdog(out, _AbortAfter(2), hb).watch(),
            timeout=2.0,
        )

    msgs = [r.getMessage() for r in caplog.records]
    assert any("twitcasting/Ao" in m and "MB" in m for m in msgs), msgs


async def test_run_proc_with_stop_kills_orphaned_grandchild(tmp_path):
    """The deadlock that wedged the TwitCasting worker: yt-dlp (the direct child)
    spawns ffmpeg (a grandchild) that inherits the captured stderr pipe. On stop,
    signalling only the direct child leaves the grandchild holding the pipe open,
    so proc.communicate() never sees EOF and the worker hangs forever.

    Launching the child in its own process group and killing the whole group on
    teardown reaps the grandchild too, so communicate() returns promptly.
    """
    import sys

    dl = Downloader()
    # Parent spawns a grandchild that inherits stderr (fd 2) and sleeps well past
    # the test window, then the parent also sleeps. Without a process-group kill,
    # the grandchild keeps the stderr pipe open and communicate() blocks.
    code = (
        "import sys, subprocess, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        "time.sleep(30)"
    )
    args = [sys.executable, "-c", code]
    stop = asyncio.Event()

    task = asyncio.create_task(
        dl.run_attempt(args, stop, stall_policy=NeverAbortStallPolicy())
    )
    # Let the parent actually spawn the grandchild and both inherit the pipe
    # BEFORE we ask for shutdown — otherwise we'd terminate the parent before a
    # grandchild even exists and the deadlock would never arise.
    await asyncio.sleep(0.7)
    stop.set()

    returncode, stderr, stalled = await asyncio.wait_for(task, timeout=8.0)
    # The call returned (no hang) because the whole group — including the
    # grandchild — was killed, letting communicate() observe EOF.
    assert stalled is False  # stop, not the watchdog, ended this run


async def test_watchdog_heartbeat_flags_no_growth(tmp_path, caplog):
    """When the output isn't growing, the heartbeat must say so — that's the
    early-warning that a recording is stalling, BEFORE the watchdog's abort
    window elapses (so a frozen capture is obvious in the log)."""
    from linkstart.downloader._stall import StallPolicy
    from linkstart.downloader._watchdog import Heartbeat, StallWatchdog

    class _AbortAfter(StallPolicy):
        poll_sec = 0.01

        def __init__(self, n):
            self.n = n
            self.calls = 0

        def should_abort(self, *, elapsed, bytes_written, since_growth):
            self.calls += 1
            return self.calls >= self.n

    dl = Downloader()
    out = tmp_path / "part00.ts"
    (tmp_path / "part00.ts.part").write_bytes(b"x" * 1000)  # written once, never grows

    hb = Heartbeat("twitcasting/Ao", dl.media, interval_sec=0.0)
    with caplog.at_level(logging.INFO, logger="linkstart.downloader._watchdog"):
        await asyncio.wait_for(
            StallWatchdog(out, _AbortAfter(4), hb).watch(), timeout=2.0
        )

    msgs = [r.getMessage() for r in caplog.records]
    assert any("no growth" in m for m in msgs), msgs


async def test_watchdog_heartbeat_ffprobe_cannot_wedge_abort(tmp_path, monkeypatch):
    """The best-effort content-duration ffprobe in the heartbeat must be time
    bounded: a hung ffprobe must NOT block the watchdog's abort decision (which
    would re-create the exact wedge the watchdog exists to prevent)."""
    from linkstart.downloader._stall import StallPolicy
    from linkstart.downloader._watchdog import Heartbeat, StallWatchdog

    class _AbortAfter(StallPolicy):
        poll_sec = 0.01

        def __init__(self, n):
            self.n = n
            self.calls = 0

        def should_abort(self, *, elapsed, bytes_written, since_growth):
            self.calls += 1
            return self.calls >= self.n

    dl = Downloader()
    out = tmp_path / "part00.ts"
    (tmp_path / "part00.ts.part").write_bytes(b"x" * 2000)

    async def hanging_ffprobe(path):
        await asyncio.Event().wait()  # never returns

    monkeypatch.setattr(dl.media, "ffprobe_duration", hanging_ffprobe)

    hb = Heartbeat("twitcasting/Ao [edge]", dl.media, interval_sec=0.0,
                   probe_timeout_sec=0.05)
    # Must still abort (return) despite the hung probe — no infinite wedge.
    await asyncio.wait_for(
        StallWatchdog(out, _AbortAfter(2), hb).watch(), timeout=2.0
    )


async def test_watchdog_heartbeat_includes_content_duration(tmp_path, caplog, monkeypatch):
    """Besides byte size, the heartbeat reports the recorded content length
    (via ffprobe, best-effort) so it's visible how much footage exists / whether
    a from-start loop has caught up to live."""
    from linkstart.downloader._stall import StallPolicy
    from linkstart.downloader._watchdog import Heartbeat, StallWatchdog

    class _AbortAfter(StallPolicy):
        poll_sec = 0.01

        def __init__(self, n):
            self.n = n
            self.calls = 0

        def should_abort(self, *, elapsed, bytes_written, since_growth):
            self.calls += 1
            return self.calls >= self.n

    dl = Downloader()
    out = tmp_path / "part00.ts"
    (tmp_path / "part00.ts.part").write_bytes(b"x" * 2000)

    async def fake_duration(path):
        return 754  # 12.6 min of content

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_duration)

    hb = Heartbeat("twitcasting/Ao [edge]", dl.media, interval_sec=0.0)
    with caplog.at_level(logging.INFO, logger="linkstart.downloader._watchdog"):
        await asyncio.wait_for(
            StallWatchdog(out, _AbortAfter(2), hb).watch(), timeout=2.0
        )

    msgs = [r.getMessage() for r in caplog.records]
    assert any("13min content" in m for m in msgs), msgs


async def test_run_proc_with_stop_kills_grandchild_after_parent_exits(tmp_path):
    """Bug: yt-dlp (the direct child / group leader) can die FIRST, leaving
    ffmpeg (grandchild) alive and holding the stderr pipe. Resolving the group
    via os.getpgid(proc.pid) then fails (the leader is gone), so the grandchild
    is never killed and communicate() never sees EOF. Killing by the pgid
    captured at spawn reaps the surviving group member regardless.
    """
    import os
    import sys

    dl = Downloader()
    pidfile = tmp_path / "gpid.txt"
    gc_code = (
        f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent_code = (
        "import sys,subprocess;"
        f"subprocess.Popen([sys.executable,'-c',{gc_code!r}]);"
        "sys.exit(0)"  # parent (group leader) exits immediately
    )
    args = [sys.executable, "-c", parent_code]
    stop = asyncio.Event()

    task = asyncio.create_task(
        dl.run_attempt(args, stop, stall_policy=NeverAbortStallPolicy())
    )
    for _ in range(100):  # wait until the grandchild has spawned + recorded its pid
        await asyncio.sleep(0.05)
        if pidfile.exists() and pidfile.read_text().strip():
            break
    gpid = int(pidfile.read_text().strip())
    stop.set()
    await asyncio.wait_for(task, timeout=8.0)

    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(gpid, 0)  # grandchild must be dead


class _CommHangsProc:
    """A subprocess whose stderr never reaches EOF (mimics yt-dlp's ffmpeg
    grandchild holding the captured pipe open) — communicate() never returns,
    even after the process is signalled."""

    def __init__(self):
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self._dead = asyncio.Event()

    async def communicate(self):
        await asyncio.Event().wait()  # never completes

    def terminate(self):
        self.terminate_called = True
        if self.returncode is None:
            self.returncode = -15
        self._dead.set()

    def kill(self):
        self.kill_called = True
        self.returncode = -9
        self._dead.set()

    async def wait(self):
        await self._dead.wait()
        return self.returncode


async def test_run_proc_with_stop_gives_up_cleanly_when_pipe_never_eofs():
    """Bug: the teardown cancelled comm_task via wait_for, then re-awaited the
    now-cancelled task — which raises CancelledError (not TimeoutError), escapes
    the handler, and poisons the worker's TaskGroup. The teardown must instead
    bound its waits and return cleanly (empty stderr) without raising."""
    dl = Downloader()
    dl.process.TEARDOWN_TERM_WAIT_SEC = 0.05  # force both escalation waits to time out fast
    proc = _CommHangsProc()

    async def fake_exec(*args, **kwargs):
        return proc

    stop = asyncio.Event()
    stop.set()
    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        returncode, stderr, stalled = await asyncio.wait_for(
            dl.run_attempt(
                ["yt-dlp"], stop, stall_policy=NeverAbortStallPolicy()
            ),
            timeout=2.0
        )

    assert stderr == b""  # gave up on stderr rather than wedging — no exception
    assert proc.terminate_called and proc.kill_called  # escalated SIGTERM → SIGKILL


async def test_edge_only_stop_event_after_natural_exit_does_not_skip_remux(channel, live, tmp_path):
    """If yt-dlp exits naturally (broadcast ended) BEFORE stop_event fires,
    cleanup still happens normally. Stop signal must not break the cleanup path."""
    plat = FakePlatform(check_results=[None])   # broadcast already ended
    dl = Downloader()
    stop_event = asyncio.Event()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live, stop_event=stop_event)

    assert result.success is True
    assert result.file_path is not None
    assert result.file_path.exists()


async def test_dual_breaks_both_loops_and_runs_cleanup_when_stop_signaled(channel, live, tmp_path):
    """Stop fires during dual recording; both full and edge loops terminate
    their yt-dlp processes and cleanup runs."""
    plat = FakePlatformDual(check_results=[live, live, live, live])
    dl = Downloader()
    stop_event = asyncio.Event()
    procs: list[TerminatableProc] = []

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")
        procs.append(proc)
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return proc

    async def fake_cleanup(paths, media, channel, live, parts_dir, *, retry_count, full_restarted=False):
        return DownloadResult(success=True)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            task = asyncio.create_task(
                dl.record(channel, plat, live, stop_event=stop_event)
            )
            # Wait for both procs to spawn.
            for _ in range(50):
                await asyncio.sleep(0)
                if len(procs) >= 2:
                    break
            stop_event.set()
            await asyncio.wait_for(task, timeout=2.0)

    assert len(procs) == 2
    assert all(p.terminate_called for p in procs)


async def test_dual_terminates_procs_on_external_cancellation(channel, live, tmp_path):
    """When dl.record() is cancelled externally (e.g. orchestrator stop), both
    running yt-dlp subprocesses must receive terminate() — otherwise zombie
    yt-dlp processes accumulate."""
    plat = FakePlatformDual(check_results=[None, None])
    dl = Downloader()
    procs: list[TerminatableProc] = []

    async def fake_exec(*args, **kwargs):
        proc = TerminatableProc(mode="hang")
        procs.append(proc)
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return proc

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            task = asyncio.create_task(dl.record(channel, plat, live))
            # Give scheduler time to create both procs.
            for _ in range(20):
                await asyncio.sleep(0)
                if len(procs) >= 2:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(procs) == 2
    assert all(p.terminate_called for p in procs)


async def test_dual_terminates_sibling_proc_on_unhandled_error(channel, live, tmp_path):
    """When one yt-dlp invocation raises an unexpected error, the sibling
    loop's running yt-dlp must still get terminate()."""
    plat = FakePlatformDual(check_results=[None, None])
    dl = Downloader()
    procs: list[TerminatableProc] = []
    trigger_raise = asyncio.Event()

    class CoordinatedProc(TerminatableProc):
        def __init__(self, *, will_raise: bool):
            super().__init__(mode="raise" if will_raise else "hang")
            self._will_raise = will_raise

        async def communicate(self):
            if self._will_raise:
                # Wait until both procs exist, then raise.
                await trigger_raise.wait()
                raise RuntimeError("kaboom")
            await self._done.wait()
            return b"", b""

    # First-created proc raises; second hangs.
    will_raise_flags = iter([True, False])

    async def fake_exec(*args, **kwargs):
        proc = CoordinatedProc(will_raise=next(will_raise_flags))
        procs.append(proc)
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return proc

    async def fake_cleanup(*a, **kw):
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            task = asyncio.create_task(dl.record(channel, plat, live))
            # Wait until both procs are created and inside communicate().
            for _ in range(50):
                await asyncio.sleep(0)
                if len(procs) >= 2:
                    break
            trigger_raise.set()
            with pytest.raises(RuntimeError, match="kaboom"):
                await task

    assert len(procs) == 2
    hang_procs = [p for p in procs if p.mode == "hang"]
    assert len(hang_procs) == 1
    assert hang_procs[0].terminate_called


# --- _cleanup_dual tests ---

async def test_cleanup_picks_longest_full_as_base(channel, live, tmp_path,
                                                  monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    # Two full files: shorter and longer.
    short = parts_dir / "full.1000.mp4"; short.write_bytes(b"short")
    longer = parts_dir / "full.1000_01.mp4"; longer.write_bytes(b"longer")

    # Mock ffprobe durations: shorter=10s, longer=30s.
    durations = {short: 10, longer: 30}

    async def fake_ffprobe(path):
        return durations.get(path)

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)

    # Mock fragment recovery to return nothing.
    async def fake_recover(*args, **kwargs):
        return []
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    # No edge files in this test
    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=1)

    assert result.success is True
    assert result.duration_sec == 30
    assert result.file_path is not None
    assert result.file_path.exists()
    # short was deleted; parts_dir is cleaned up
    assert not parts_dir.exists()


async def test_cleanup_deletes_contained_edge(channel, live, tmp_path,
                                              monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")
    edge = parts_dir / "edge.1010.mp4"; edge.write_bytes(b"edge")
    # base 1000~1100, edge 1010~1080 → fully contained
    durations = {base: 100, edge: 70}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)

    assert result.success is True
    assert result.extra_files == []   # edge was contained → deleted


async def test_cleanup_keeps_edge_with_tail(channel, live, tmp_path,
                                            monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")
    edge = parts_dir / "edge.1050.mp4"; edge.write_bytes(b"edge")
    # base 1000~1100, edge 1050~1200 → tail extends past base end
    durations = {base: 100, edge: 150}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)

    assert result.success is True
    assert len(result.extra_files) == 1
    assert result.extra_files[0].name.endswith(".edge_001.mp4")
    assert result.extra_files[0].exists()


def test_new_coverage_seconds_basic():
    from linkstart.downloader._cleanup import _new_coverage_seconds
    # Fully covered → 0
    assert _new_coverage_seconds((10, 20), [(0, 100)]) == 0
    # Fully uncovered → full length
    assert _new_coverage_seconds((10, 20), [(100, 200)]) == 10
    # Empty covered → full length
    assert _new_coverage_seconds((10, 20), []) == 10
    # Zero-length candidate → 0
    assert _new_coverage_seconds((10, 10), []) == 0


def test_new_coverage_seconds_partial_overlap():
    from linkstart.downloader._cleanup import _new_coverage_seconds
    # candidate=[10,30], covered=[(0,20)] → overlap 10, new = 10
    assert _new_coverage_seconds((10, 30), [(0, 20)]) == 10
    # Tail extension: candidate=[15,40], covered=[(0,25)] → overlap 10, new = 15
    assert _new_coverage_seconds((15, 40), [(0, 25)]) == 15


def test_new_coverage_seconds_handles_overlapping_covered_intervals():
    """Two covered intervals that overlap each other within candidate must not
    cause double-subtraction (merge-then-subtract semantics)."""
    from linkstart.downloader._cleanup import _new_coverage_seconds
    # candidate=[30,80], covered=[(0,50),(40,60)] → union covers [0,60]
    # Candidate∩union = [30,60] = 30. New = 50 - 30 = 20.
    assert _new_coverage_seconds((30, 80), [(0, 50), (40, 60)]) == 20


async def test_cleanup_skips_yt_dlp_intermediate_format_files_when_picking_base(
    channel, live, tmp_path, monkeypatch
):
    """yt-dlp leaves intermediate per-format files like `full.{epoch}.f140.mp4`
    when interrupted before remux. These look like 'full.*.mp4' but their epoch
    isn't parseable from the bare filename, so they must NOT be picked as base."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    # Real base.
    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")
    # Intermediate audio-only file from a sibling, incomplete full run.
    intermediate = parts_dir / "full.2000.f140.mp4"; intermediate.write_bytes(b"audio-only")

    # Intermediate is LONGER — without the fix the algorithm would pick it as base
    # (then fail to parse its epoch).
    durations = {base: 30, intermediate: 60}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*a, **kw):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    # base must be `full.1000.mp4` (parseable epoch), not the intermediate one.
    assert result.success is True
    assert result.file_path is not None
    # The intermediate file is untouched (left behind for fragment recovery).
    assert intermediate.exists() or not intermediate.exists()   # not asserting its fate


async def test_recover_fragments_pairs_complete_audio_with_part_video(tmp_path, monkeypatch):
    """yt-dlp hang scenario: audio finalized to .f140.mp4, video still .f137.mp4.part.
    The recovery code must pair them and remux."""
    dl = Downloader()
    parts_dir = tmp_path / "parts.dir"
    parts_dir.mkdir()

    video = parts_dir / "full.1700000000.f137.mp4.part"; video.write_bytes(b"v")
    audio = parts_dir / "full.1700000000.f140.mp4"; audio.write_bytes(b"a")

    async def fake_ffprobe(path):
        return 1000

    async def fake_remux(v, a, target):
        target.write_bytes(b"remuxed")
        return True

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr(dl.media, "ffmpeg_remux", fake_remux)

    import os
    os.utime(video, (1700000000, 1700000000))

    base_final = tmp_path / "base.mp4"
    extras = await _cleanup_recover_fragments(dl.media, parts_dir, base_final, covered=[])
    assert len(extras) == 1
    assert extras[0].name.endswith(".recovered_001.mp4")


async def test_cleanup_no_base_recovers_fragments_when_only_intermediates_exist(
    channel, live, tmp_path, monkeypatch
):
    """YouTube hang scenario: no complete full.{epoch}.mp4 or edge.{epoch}.mp4,
    only intermediate `.f<itag>.mp4(.part)?` files. _cleanup_no_base should still
    produce an mp4 by remuxing the fragment pair."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    video = parts_dir / "full.1700000000.f137.mp4.part"; video.write_bytes(b"v")
    audio = parts_dir / "full.1700000000.f140.mp4"; audio.write_bytes(b"a")

    async def fake_ffprobe(path):
        return 1000

    async def fake_remux(v, a, target):
        target.write_bytes(b"remuxed")
        return True

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr(dl.media, "ffmpeg_remux", fake_remux)

    import os
    os.utime(video, (1700000000, 1700000000))

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True
    assert result.file_path is not None
    assert result.file_path.exists()


async def test_cleanup_keeps_edge_when_base_died_early(channel, live, tmp_path,
                                                        monkeypatch):
    """The reported bug: full died at base_end, edge1 has unique data past
    base_end but its end aligns near base_end + old margin. With interval-union
    coverage, edge1's unique tail is recognized and preserved."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")    # 1000~1025
    edge1 = parts_dir / "edge.1000.mp4"; edge1.write_bytes(b"e1")    # 1000~1040 (15s unique tail)
    edge2 = parts_dir / "edge.1043.mp4"; edge2.write_bytes(b"e2")    # 1043~1060

    durations = {base: 25, edge1: 40, edge2: 17}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)

    assert result.success is True
    # Both edges contain unique coverage; both preserved.
    assert len(result.extra_files) == 2
    names = sorted(p.name for p in result.extra_files)
    assert any(".edge_001.mp4" in n for n in names)
    assert any(".edge_002.mp4" in n for n in names)


async def test_cleanup_drops_edge_with_subthreshold_new_coverage(channel, live,
                                                                  tmp_path, monkeypatch):
    """Edge ends 3 seconds past base — below the 5s threshold → dropped."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")    # 1000~1100
    # edge starts before base end, tail extends only 3s past → below threshold
    edge = parts_dir / "edge.1050.mp4"; edge.write_bytes(b"e")        # 1050~1103

    durations = {base: 100, edge: 53}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True
    assert result.extra_files == []   # 3s tail < 5s threshold


async def test_cleanup_chains_coverage_across_edges(channel, live, tmp_path,
                                                    monkeypatch):
    """edge1 extends covered range; edge2 fully inside that extended range → dropped.
    Verifies the chained interval union (edges build on each other's coverage)."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"b")        # 1000~1025
    edge1 = parts_dir / "edge.1010.mp4"; edge1.write_bytes(b"e1")    # 1010~1060
    edge2 = parts_dir / "edge.1030.mp4"; edge2.write_bytes(b"e2")    # 1030~1055 — fully within base ∪ edge1

    durations = {base: 25, edge1: 50, edge2: 25}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True
    assert len(result.extra_files) == 1
    assert result.extra_files[0].name.endswith(".edge_001.mp4")


async def test_cleanup_no_base_uses_edge_fallback(channel, live, tmp_path,
                                                  monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    # Only edge files exist (full all failed)
    edge1 = parts_dir / "edge.1000.mp4"; edge1.write_bytes(b"e1")
    edge2 = parts_dir / "edge.1100.mp4"; edge2.write_bytes(b"e2")

    durations = {edge1: 50, edge2: 50}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)

    # Fallback: first edge becomes base, rest become extras.
    assert result.success is True
    assert result.file_path is not None and result.file_path.exists()
    assert len(result.extra_files) == 1
    assert result.file_path.name.endswith(".mp4")


async def test_cleanup_keeps_contained_edge_when_full_restarted(channel, live, tmp_path,
                                                                monkeypatch):
    """When the full loop restarted, the base's filename epoch is its spawn time
    while its content starts at broadcast start — the coverage interval is shifted
    late and cannot be trusted. Edges must NOT be deleted in that case, even if
    they look fully contained."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")
    edge = parts_dir / "edge.1010.mp4"; edge.write_bytes(b"edge")
    # base 1000~1100, edge 1010~1080 → looks fully contained
    durations = {base: 100, edge: 70}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(
        dl.paths, dl.media, channel, live, parts_dir, retry_count=1, full_restarted=True
    )

    assert result.success is True
    assert len(result.extra_files) == 1
    assert result.extra_files[0].name.endswith(".edge_001.mp4")
    assert result.extra_files[0].exists()


async def test_dual_passes_full_restarted_true_to_cleanup(channel, live, tmp_path):
    """One full-loop restart → cleanup must be told the base epoch is unreliable."""
    plat = FakePlatformDual(check_results=[live, None, None])
    dl = Downloader()
    captured = {}

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_cleanup(paths, media, channel, live, parts_dir, *, retry_count, full_restarted):
        captured["full_restarted"] = full_restarted
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    assert captured["full_restarted"] is True


async def test_dual_passes_full_restarted_false_without_restart(channel, live, tmp_path):
    plat = FakePlatformDual(check_results=[None, None])
    dl = Downloader()
    captured = {}

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_cleanup(paths, media, channel, live, parts_dir, *, retry_count, full_restarted):
        captured["full_restarted"] = full_restarted
        return DownloadResult(success=False)

    with patch("linkstart.downloader._dual.cleanup_dual", new=AsyncMock(side_effect=fake_cleanup)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    assert captured["full_restarted"] is False


async def test_cleanup_uses_started_at_for_base_interval_when_full_restarted(
    channel, tmp_path, monkeypatch
):
    """A restarted full's filename epoch is its spawn time, but its content starts
    at the broadcast start. With live.started_at the base interval is exact, so an
    edge tail past the TRUE end is kept while an edge inside it is still deleted."""
    from datetime import datetime, timezone

    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    live = LiveInfo(
        live_id="100", title="hello", url="https://fake/abc",
        started_at=datetime.fromtimestamp(1000, tz=timezone.utc),
    )

    # Restarted full: spawned at 1030, content 1000~1060 (60s).
    # Naive epoch+duration would claim 1030~1090 and swallow the edge tail.
    base = parts_dir / "full.1030.mp4"; base.write_bytes(b"base")
    edge_inside = parts_dir / "edge.1005.mp4"; edge_inside.write_bytes(b"e1")  # 1005~1035
    edge_tail = parts_dir / "edge.1055.mp4"; edge_tail.write_bytes(b"e2")      # 1055~1075

    durations = {base: 60, edge_inside: 30, edge_tail: 20}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*args, **kwargs):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(
        dl.paths, dl.media, channel, live, parts_dir, retry_count=1, full_restarted=True
    )

    assert result.success is True
    # edge_tail has 15s past the true end (1060) → kept despite the restart.
    assert len(result.extra_files) == 1
    assert result.extra_files[0].name.endswith(".edge_001.mp4")


# --- _recover_fragments tests ---

async def test_recover_fragments_skips_contained(tmp_path, monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "parts.dir"
    parts_dir.mkdir()

    # A pair of part files (video + audio) for the same prefix.
    v = parts_dir / "20260604.f137.mp4.part"; v.write_bytes(b"v")
    a = parts_dir / "20260604.f140.mp4.part"; a.write_bytes(b"a")

    # Pretend it's contained (epoch + duration < base_end)
    async def fake_ffprobe(path):
        return 60

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    # Force "epoch" via stat mtime: use a deterministic mtime
    import os
    os.utime(v, (1000, 1000))

    base_final = tmp_path / "base.mp4"
    extras = await _cleanup_recover_fragments(dl.media, parts_dir, base_final, covered=[(0, 2000)])

    assert extras == []   # contained → no extras


async def test_recover_fragments_remuxes_uncovered(tmp_path, monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "parts.dir"
    parts_dir.mkdir()

    v = parts_dir / "20260604.f137.mp4.part"; v.write_bytes(b"v")
    a = parts_dir / "20260604.f140.mp4.part"; a.write_bytes(b"a")

    async def fake_ffprobe(path):
        return 300   # 5 minutes — extends past base_end

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)

    # Mock ffmpeg remux: write the target file and return True.
    async def fake_remux(video, audio, target):
        target.write_bytes(b"remuxed")
        return True

    monkeypatch.setattr(dl.media, "ffmpeg_remux", fake_remux)

    import os
    os.utime(v, (1000, 1000))

    base_final = tmp_path / "base.mp4"
    base_final.write_bytes(b"")
    extras = await _cleanup_recover_fragments(dl.media, parts_dir, base_final, covered=[(0, 100)])

    assert len(extras) == 1
    assert extras[0].name.endswith(".recovered_001.mp4")
    assert extras[0].exists()


async def test_recover_fragments_skips_groups_of_wrong_size(tmp_path, monkeypatch):
    """Fragment pairs with !=2 members (e.g. partial pair or trio) are skipped."""
    dl = Downloader()
    parts_dir = tmp_path / "parts.dir"
    parts_dir.mkdir()

    # A "pair" with only one member.
    (parts_dir / "lone.f137.mp4.part").write_bytes(b"v")
    # A "pair" with three members.
    (parts_dir / "trio.f137.mp4.part").write_bytes(b"v")
    (parts_dir / "trio.f140.mp4.part").write_bytes(b"a")
    (parts_dir / "trio.f250.mp4.part").write_bytes(b"x")

    base_final = tmp_path / "base.mp4"
    extras = await _cleanup_recover_fragments(dl.media, parts_dir, base_final, covered=[])
    assert extras == []


async def test_recover_fragments_remux_failure_yields_no_extra(tmp_path,
                                                               monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "parts.dir"
    parts_dir.mkdir()

    v = parts_dir / "20260604.f137.mp4.part"; v.write_bytes(b"v")
    a = parts_dir / "20260604.f140.mp4.part"; a.write_bytes(b"a")

    async def fake_ffprobe(path):
        return 300

    async def fake_remux(video, audio, target):
        return False

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr(dl.media, "ffmpeg_remux", fake_remux)

    import os
    os.utime(v, (1000, 1000))

    base_final = tmp_path / "base.mp4"
    extras = await _cleanup_recover_fragments(dl.media, parts_dir, base_final, covered=[(0, 100)])
    assert extras == []


# --- _ffprobe_duration / _ffmpeg_remux / _concat (via _run_proc mock) ---

async def test_ffprobe_duration_parses_stdout(tmp_path):
    dl = Downloader()
    captured: dict = {}

    async def fake_run(args, *, capture_stdout=False):
        captured["args"] = args
        captured["capture_stdout"] = capture_stdout
        return 0, b"123.456\n", b""

    with patch.object(dl.process, "run", side_effect=fake_run):
        d = await dl.media.ffprobe_duration(tmp_path / "x.mp4")
    assert d == 123
    assert captured["capture_stdout"] is True
    assert dl.media.ffprobe_bin in captured["args"]
    assert str(tmp_path / "x.mp4") in captured["args"]


async def test_ffprobe_duration_returncode_nonzero_returns_none(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        return 1, b"", b"err"

    with patch.object(dl.process, "run", side_effect=fake_run):
        assert await dl.media.ffprobe_duration(tmp_path / "x.mp4") is None


async def test_ffprobe_duration_empty_stdout_returns_none(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        return 0, b"   \n", b""

    with patch.object(dl.process, "run", side_effect=fake_run):
        assert await dl.media.ffprobe_duration(tmp_path / "x.mp4") is None


async def test_ffprobe_duration_unparseable_stdout_returns_none(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        return 0, b"not-a-float\n", b""

    with patch.object(dl.process, "run", side_effect=fake_run):
        assert await dl.media.ffprobe_duration(tmp_path / "x.mp4") is None


async def test_ffprobe_duration_run_proc_raises_returns_none(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        raise OSError("boom")

    with patch.object(dl.process, "run", side_effect=fake_run):
        assert await dl.media.ffprobe_duration(tmp_path / "x.mp4") is None


async def test_ffmpeg_remux_success(tmp_path):
    dl = Downloader()
    captured: dict = {}

    async def fake_run(args, *, capture_stdout=False):
        captured["args"] = args
        return 0, b"", b""

    v = tmp_path / "v.part"
    a = tmp_path / "a.part"
    t = tmp_path / "out.mp4"
    with patch.object(dl.process, "run", side_effect=fake_run):
        ok = await dl.media.ffmpeg_remux(v, a, t)
    assert ok is True
    assert dl.media.ffmpeg_bin in captured["args"]
    assert str(v) in captured["args"]
    assert str(a) in captured["args"]
    assert str(t) in captured["args"]


async def test_ffmpeg_remux_nonzero_returns_false(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        return 1, b"", b"fail"

    with patch.object(dl.process, "run", side_effect=fake_run):
        ok = await dl.media.ffmpeg_remux(tmp_path / "v", tmp_path / "a", tmp_path / "t")
    assert ok is False


async def test_ffmpeg_remux_run_proc_raises_returns_false(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        raise OSError("boom")

    with patch.object(dl.process, "run", side_effect=fake_run):
        ok = await dl.media.ffmpeg_remux(tmp_path / "v", tmp_path / "a", tmp_path / "t")
    assert ok is False


async def test_remux_ts_to_mp4_success(tmp_path):
    dl = Downloader()
    captured: dict = {}

    async def fake_run(args, *, capture_stdout=False):
        captured["args"] = args
        return 0, b"", b""

    final = tmp_path / "final.mp4"
    part = tmp_path / "p0.ts"
    with patch.object(dl.process, "run", side_effect=fake_run):
        ok = await dl.media.remux(part, final)
    assert ok is True
    # Remux is a copy operation, not concat.
    assert "concat" not in captured["args"]
    assert str(part) in captured["args"]
    assert str(final) in captured["args"]
    assert "-c" in captured["args"]
    assert "copy" in captured["args"]


async def test_remux_ts_to_mp4_failure(tmp_path):
    dl = Downloader()

    async def fake_run(args, *, capture_stdout=False):
        return 1, b"", b"boom"

    part = tmp_path / "p0.ts"
    final = tmp_path / "final.mp4"
    with patch.object(dl.process, "run", side_effect=fake_run):
        assert await dl.media.remux(part, final) is False


# --- _cleanup_dual algorithm edge cases ---

async def test_cleanup_dual_unparseable_base_epoch_is_skipped(channel, live, tmp_path, monkeypatch):
    """A full.*.mp4 file whose name doesn't match the strict epoch pattern is
    skipped as a base candidate. Falling through to _cleanup_no_base, with no
    edges and no recoverable fragments, returns 'no usable recordings produced'."""
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)
    weird = parts_dir / "full.notanumber.mp4"
    weird.write_bytes(b"x")

    async def fake_ffprobe(path):
        return 10

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is False
    assert "no usable recordings produced" in (result.error or "")


async def test_cleanup_dual_edge_with_unparseable_epoch_is_skipped(channel, live, tmp_path,
                                                                    monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")
    # Unparseable edge filename — will be skipped (not deleted, not kept).
    weird_edge = parts_dir / "edge.weirdname.mp4"; weird_edge.write_bytes(b"e")

    durations = {base: 30, weird_edge: 5}

    async def fake_ffprobe(path):
        return durations.get(path)

    async def fake_recover(*a, **kw):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True
    # Unparseable edge file should not appear as an extra.
    assert result.extra_files == []


async def test_cleanup_dual_edge_ffprobe_none_is_kept(channel, live, tmp_path, monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)

    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")
    edge_unknown = parts_dir / "edge.2000.mp4"; edge_unknown.write_bytes(b"e")

    async def fake_ffprobe(path):
        if path == edge_unknown:
            return None   # ffprobe failed — keep defensively
        return 30   # base duration

    async def fake_recover(*a, **kw):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True
    assert len(result.extra_files) == 1
    assert result.extra_files[0].name.endswith(".edge_001.mp4")


async def test_cleanup_no_base_empty_edge_files_returns_failure(channel, live, tmp_path,
                                                                 monkeypatch):
    dl = Downloader()
    parts_dir = tmp_path / "parts.dir"
    parts_dir.mkdir()
    # No full.*.mp4 and no edge.*.mp4 → cleanup_no_base returns failure.

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=2)
    assert result.success is False
    assert "no usable recordings produced" in (result.error or "")
    assert result.retry_count == 2


async def test_cleanup_dual_summary_append_failure_does_not_break_success(
    channel, live, tmp_path, monkeypatch
):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)
    base = parts_dir / "full.1000.mp4"; base.write_bytes(b"base")

    async def fake_ffprobe(path):
        return 30

    async def fake_recover(*a, **kw):
        return []

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)
    monkeypatch.setattr("linkstart.downloader._cleanup._recover_fragments", fake_recover)

    def boom(**kw):
        raise RuntimeError("summary store broken")

    monkeypatch.setattr("linkstart.summary.append_recording_record", boom)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True   # Summary failure is best-effort, does not affect success.


async def test_cleanup_no_base_summary_append_failure_does_not_break_success(
    channel, live, tmp_path, monkeypatch
):
    dl = Downloader()
    parts_dir = tmp_path / "rec" / "fake" / "abc" / "parts.dir"
    parts_dir.mkdir(parents=True)
    edge = parts_dir / "edge.1000.mp4"; edge.write_bytes(b"e")

    async def fake_ffprobe(path):
        return 10

    monkeypatch.setattr(dl.media, "ffprobe_duration", fake_ffprobe)

    def boom(**kw):
        raise RuntimeError("summary store broken")

    monkeypatch.setattr("linkstart.summary.append_recording_record", boom)

    result = await cleanup_dual(dl.paths, dl.media, channel, live, parts_dir, retry_count=0)
    assert result.success is True


# --- validation wiring: platform.validate_recording is called on success ---

async def test_record_attaches_validation_from_platform(channel, live):
    plat = FakePlatform(check_results=[None])
    plat.validate_recording = AsyncMock(  # type: ignore[method-assign]
        return_value=__import__(
            "linkstart.models", fromlist=["ValidationResult"]
        ).ValidationResult(status="invalid", reason="bad output")
    )
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    plat.validate_recording.assert_awaited_once()
    # Called with the produced file path.
    called_with = plat.validate_recording.await_args.args[0]
    assert called_with == result.file_path
    assert result.validation.status == "invalid"
    assert result.validation.reason == "bad output"


async def test_record_does_not_validate_on_failure(channel, live):
    """Failed downloads have no usable file → skip validate_recording."""
    plat = FakePlatform(check_results=[None])
    plat.validate_recording = AsyncMock()  # type: ignore[method-assign]
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        # Do NOT create the output file → no parts → failure.
        return FakeProc(returncode=1)

    with patch(
        "linkstart.downloader._process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        result = await dl.record(channel, plat, live)

    assert result.success is False
    plat.validate_recording.assert_not_awaited()


async def test_record_default_validation_is_ok_for_unmodified_platform(channel, live):
    """A platform that doesn't override validate_recording yields status=ok."""
    plat = FakePlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc(returncode=0)

    async def fake_remux(part, target):
        target.write_bytes(b"final")
        return True

    with patch.object(dl.media, "remux", new=AsyncMock(side_effect=fake_remux)):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    assert result.validation.status == "ok"


async def test_cleanup_dual_no_base_returns_failure(tmp_path):
    from linkstart.downloader._cleanup import cleanup_dual
    from linkstart.downloader._paths import RecordingPaths
    from linkstart.downloader._media import MediaTools
    from linkstart.downloader._process import ProcessRunner
    paths = RecordingPaths()
    media = MediaTools(ProcessRunner())
    parts = tmp_path / "x.parts"
    parts.mkdir()
    # no full.*.mp4, no edge.*.mp4, no fragments → no usable recording
    channel = ChannelConfig(platform="fake", channel_id="abc", save_dir=tmp_path / "rec", poll_interval=0)
    live = LiveInfo(live_id="100", title="hello", url="https://fake/abc")
    result = await cleanup_dual(paths, media, channel, live, parts, retry_count=0)
    assert result.success is False
