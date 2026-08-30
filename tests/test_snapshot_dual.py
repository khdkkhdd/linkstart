"""Tests for SnapshotDualRecordingStrategy — full = VOD snapshots at
detection and after broadcast end, edge = live loop, merged by cleanup_dual."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from linkstart.downloader import Downloader
from linkstart.downloader._snapshot_dual import SnapshotDualRecordingStrategy
from linkstart.models import ChannelConfig, DownloadProfile, LiveInfo
from linkstart.platforms.base import Platform

EDGE_URL = "https://edge.example/live.m3u8"
FULL_URL = "https://vod.example/media/hls/master.m3u8"


class FakeSnapshotPlatform(Platform):
    name = "fake"

    def __init__(self, *, check_results, full_url=FULL_URL):
        self._results = list(check_results)
        self._full_url = full_url

    async def check_live(self, channel):
        return self._results.pop(0) if self._results else None

    def build_url(self, channel, live):
        return EDGE_URL

    def build_full_url(self, channel, live):
        return self._full_url

    def recording_strategy(self, ctx):
        return SnapshotDualRecordingStrategy(ctx)

    def _base_download_profile(self, channel):
        return DownloadProfile(
            container="mpegts",
            downloader="native",
            extra_args=("--referer", "https://fake/page"),
        )


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode

    async def communicate(self):
        return b"", b""


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


def _fake_exec(calls, payload_by_url):
    """create_subprocess_exec stub: record (url, output), write the payload."""

    async def fake_exec(*args, **kwargs):
        url = args[-1]
        out_idx = args.index("-o")
        output = Path(args[out_idx + 1])
        calls.append((url, output.name))
        output.write_bytes(payload_by_url.get(url, b"data"))
        return FakeProc()

    return fake_exec


def _duration_by_size(scale=1):
    async def fake_duration(path):
        return path.stat().st_size * scale

    return fake_duration


async def _run(dl, channel, plat, live, calls, payload_by_url):
    with patch.object(
        dl.media, "ffprobe_duration",
        new=AsyncMock(side_effect=_duration_by_size()),
    ):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_exec(calls, payload_by_url)),
        ):
            return await dl.record(channel, plat, live)


async def test_snapshots_bracket_the_edge_recording(channel, live):
    # not live after the first edge attempt → edge loop ends, final snapshot runs
    plat = FakeSnapshotPlatform(check_results=[None])
    dl = Downloader()
    calls: list[tuple[str, str]] = []

    result = await _run(
        dl, channel, plat, live, calls,
        # final snapshot (2nd FULL_URL call) is the longest → becomes base
        {EDGE_URL: b"edge-data", FULL_URL: b"full-data-longer"},
    )

    assert result.success is True
    full_calls = [c for c in calls if c[0] == FULL_URL]
    edge_calls = [c for c in calls if c[0] == EDGE_URL]
    assert len(full_calls) == 2, calls   # initial + final snapshot
    assert len(edge_calls) == 1, calls
    assert all(name.startswith("full.") for _, name in full_calls)
    assert all(name.startswith("edge.") for _, name in edge_calls)


async def test_snapshot_args_drop_mpegts_but_keep_referer(channel, live):
    plat = FakeSnapshotPlatform(check_results=[None])
    dl = Downloader()
    seen_args: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        seen_args.append(list(args))
        out_idx = args.index("-o")
        Path(args[out_idx + 1]).write_bytes(b"data")
        return FakeProc()

    with patch.object(
        dl.media, "ffprobe_duration",
        new=AsyncMock(side_effect=_duration_by_size()),
    ):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            await dl.record(channel, plat, live)

    snapshot_args = [a for a in seen_args if a[-1] == FULL_URL]
    assert snapshot_args, seen_args
    for args in snapshot_args:
        assert "--hls-use-mpegts" not in args      # snapshots are finite VODs
        assert "--hls-prefer-native" in args
        assert "--referer" in args
    edge_args = [a for a in seen_args if a[-1] == EDGE_URL]
    assert edge_args and "--hls-use-mpegts" in edge_args[0]


async def test_without_full_url_degrades_to_edge_only(channel, live):
    plat = FakeSnapshotPlatform(check_results=[None], full_url=None)
    dl = Downloader()
    calls: list[tuple[str, str]] = []

    result = await _run(
        dl, channel, plat, live, calls, {EDGE_URL: b"edge-data"},
    )

    assert result.success is True
    assert all(url == EDGE_URL for url, _ in calls), calls


async def test_snapshot_failure_still_finishes_with_edge(channel, live, monkeypatch):
    monkeypatch.setattr(
        SnapshotDualRecordingStrategy, "FINAL_SNAPSHOT_RETRY_DELAY", 0
    )
    plat = FakeSnapshotPlatform(check_results=[None])
    dl = Downloader()

    async def fake_exec(*args, **kwargs):
        url = args[-1]
        out_idx = args.index("-o")
        output = Path(args[out_idx + 1])
        if url == FULL_URL:
            return FakeProc(returncode=1)   # snapshot fails, writes nothing
        output.write_bytes(b"edge-data")
        return FakeProc()

    with patch.object(
        dl.media, "ffprobe_duration",
        new=AsyncMock(side_effect=_duration_by_size()),
    ):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ):
            result = await dl.record(channel, plat, live)

    assert result.success is True
    assert result.file_path is not None


async def test_final_snapshot_skipped_on_stop_event(channel, live):
    plat = FakeSnapshotPlatform(check_results=[None])
    dl = Downloader()
    calls: list[tuple[str, str]] = []
    stop_event = asyncio.Event()
    stop_event.set()

    with patch.object(
        dl.media, "ffprobe_duration",
        new=AsyncMock(side_effect=_duration_by_size()),
    ):
        with patch(
            "linkstart.downloader._process.asyncio.create_subprocess_exec",
            new=AsyncMock(
                side_effect=_fake_exec(calls, {EDGE_URL: b"e", FULL_URL: b"f"})
            ),
        ):
            await dl.record(channel, plat, live, stop_event=stop_event)

    # stop requested → at most the initial snapshot ran, never the final one
    assert len([c for c in calls if c[0] == FULL_URL]) <= 1, calls
